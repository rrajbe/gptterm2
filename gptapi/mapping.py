"""Map an OpenAI chat request onto a chatgpt.com conversation.

Order: (a) pinned by X-Conversation-Id header or model "chatgpt-web/<name>";
(b) automatic: hash of the history before the last message matches a known point of a stored
conversation -> send only the last message; (c) otherwise a new conversation whose first prompt
is the flattened history.
"""
import logging
import re
from dataclasses import dataclass, field

from .history import flatten, has_dialogue, history_hash, message_prompt, split_request

log = logging.getLogger("gptapi")

MODEL_BASE = "chatgpt-web"
_CONV_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class BadRequest(Exception):
    pass


@dataclass
class Plan:
    conv_id: object = None        # existing conversation to continue, None = new
    inject: object = None         # conversationState to graft (branch from an older point)
    prompt: str = ""
    name: object = None           # pinned name to bind after the turn
    mode: str = "new"             # new | named | pinned | matched | matched-nosystem | branch | rollover
    prefix: list = field(default_factory=list)
    last: dict = field(default_factory=dict)
    rolled_from: object = None    # conversation that was too full and was replaced (rollover)


def model_name(model):
    """'chatgpt-web/foo' -> 'foo'; anything else -> None (automatic mapping)."""
    if model and model.startswith(MODEL_BASE + "/"):
        n = model[len(MODEL_BASE) + 1:].strip()
        return n or None
    return None


def resolve(store, messages, model=None, header_conv=None):
    prefix, last = split_request(messages)
    if last is None:
        raise BadRequest("messages must contain at least one message with text")
    plan = Plan(prefix=prefix, last=last)
    header_conv = (header_conv or "").strip() or None
    if header_conv and hasattr(store, "follow"):
        header_conv = store.follow(header_conv)      # a rolled-over conversation continues in its successor

    # (a) pinned
    if header_conv and store.conv(header_conv) and (store.conv(header_conv) or {}).get("state"):
        plan.conv_id, plan.mode, plan.prompt = header_conv, "pinned", message_prompt(last)
        return plan
    name = None
    if header_conv:
        name = header_conv
    else:
        name = model_name(model)
    if name:
        plan.name = name
        cid = store.name_to_conv(name)
        if cid and hasattr(store, "follow"):
            cid = store.follow(cid)
        if cid and (store.conv(cid) or {}).get("state"):
            plan.conv_id, plan.mode, plan.prompt = cid, "named", message_prompt(last)
        else:
            plan.mode, plan.prompt = "new", flatten(prefix, last)
            if header_conv and _CONV_ID_RE.match(header_conv):
                log.warning("X-Conversation-Id %s is not a known conversation; starting a new one", header_conv)
        return plan

    # (b) automatic
    if has_dialogue(prefix):
        for ignore_system, mode in ((False, "matched"), (True, "matched-nosystem")):
            m = store.match(history_hash(prefix, ignore_system=ignore_system))
            if m:
                cid, state, is_latest = m
                plan.conv_id, plan.prompt = cid, message_prompt(last)
                plan.mode = mode if is_latest else "branch"
                plan.inject = None if is_latest else state
                return plan

    # (c) new
    plan.mode, plan.prompt = "new", flatten(prefix, last)
    return plan


def after_turn_hashes(prefix, last, answers):
    """History hashes a client will send next time (with each answer variant it may have kept)."""
    hs = []
    for a in answers:
        if not a:
            continue
        full = list(prefix) + [last, {"role": "assistant", "content": a}]
        hs.append(history_hash(full))
        hs.append(history_hash(full, ignore_system=True))
    return hs
