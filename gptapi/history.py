"""Pure helpers for OpenAI message lists: text extraction, hashing, flattening."""
import hashlib
import json
import re

ROLE_LABEL = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool result"}


def content_text(content):
    """OpenAI content may be a string, None, or a list of parts ({type:text,text}, images...)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text") or "text" in p:
                    out.append(str(p.get("text") or ""))
                elif p.get("type") in ("image_url", "input_image"):
                    out.append("[image omitted]")
        return "\n".join(x for x in out if x)
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return str(content)


def norm_role(role):
    role = (role or "user").lower()
    if role == "developer":
        return "system"
    if role == "function":
        return "tool"
    return role if role in ROLE_LABEL else "user"


def normalize(messages):
    """[(role, text)] with roles folded and whitespace collapsed; empty messages dropped
    (an assistant message that only carried tool_calls has no text)."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = re.sub(r"\s+", " ", content_text(m.get("content"))).strip()
        if not text:
            continue
        out.append((norm_role(m.get("role")), text))
    return out


def history_hash(messages, ignore_system=False):
    """Stable hash of a message list (already a list of dicts)."""
    norm = normalize(messages)
    if ignore_system:
        norm = [x for x in norm if x[0] != "system"]
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False).encode()).hexdigest()[:32]


def split_request(messages):
    """-> (prefix messages, last message). The last message is what gets sent to chatgpt.com."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    # the "last message" is the last one with text; trailing empty ones are dropped
    while msgs and not content_text(msgs[-1].get("content")).strip():
        msgs.pop()
    if not msgs:
        return [], None
    return msgs[:-1], msgs[-1]


def has_dialogue(prefix):
    """True if the prefix contains at least one assistant reply (worth matching against)."""
    return any(r == "assistant" for r, _ in normalize(prefix))


def message_prompt(last):
    """The text typed for a single new message (labelled if it is not a plain user message)."""
    role = norm_role(last.get("role"))
    text = content_text(last.get("content")).strip()
    if role == "user":
        return text
    return f"[{ROLE_LABEL[role]}]\n{text}"


def flatten(prefix, last):
    """First prompt of a new chatgpt.com conversation: system prompt, prior turns labelled,
    final message. A lone user message is sent as-is."""
    systems = [content_text(m.get("content")).strip() for m in prefix if norm_role(m.get("role")) == "system"]
    systems = [s for s in systems if s]
    turns = [(norm_role(m.get("role")), content_text(m.get("content")).strip()) for m in prefix
             if norm_role(m.get("role")) != "system"]
    turns = [(r, t) for r, t in turns if t]
    final = message_prompt(last)
    if not systems and not turns:
        return final
    parts = []
    if systems:
        parts.append("[System instructions]\n" + "\n\n".join(systems))
    if turns:
        parts.append("[Conversation so far]\n" + "\n\n".join(f"{ROLE_LABEL[r]}: {t}" for r, t in turns))
    parts.append("[Current message]\n" + final)
    if turns:
        parts.append("(Continue the conversation: reply to the current message only.)")
    return "\n\n".join(parts)
