"""Emulated tool calling and lean prompts for agent clients (opencode and similar).

chatgpt.com has no client tool calling, so when a request carries OpenAI `tools`, gptapi:

* rewrites the message list into plain text: assistant tool calls become ```tool_call blocks,
  tool results become "[tool result <name>]" text (long ones capped, the model pages with
  read/grep), and consecutive tool/user messages after an assistant turn merge into one message
  (one chatgpt.com message per agent step);
* replaces the client's system prompt with a short one plus the client's <env> block;
* puts the tool protocol and a one-line-per-tool catalog LAST in the first message, and a short
  reminder in every later message (chatgpt.com drops the oldest messages first);
* parses ```tool_call blocks out of the answer, checks them against the tool schemas, and
  returns them as OpenAI `tool_calls`.

All renderings are canonical, so the history hash of the next request matches the stored one.
See research/TOOL-CALLING.md, research/LIMITS.md.
"""
import json
import logging
import re
import uuid

from .history import content_text, norm_role

log = logging.getLogger("gptapi")

DEFAULT_ALLOW = ("edit", "write", "patch", "apply_patch", "glob", "grep", "read", "list", "shell")
# tools whose description defines a format the model must follow: sent in full, not one line
FULL_DESCRIPTION = ("patch", "apply_patch")

SYSTEM = """You are a coding agent working on the user's computer through a coding tool (for example opencode). \
You cannot see files or run commands yourself: you ask the tool to do it with tool calls, and the results come back \
in the next message. The tool connection is live in this conversation: the tool runs every valid tool call on the \
user's computer and sends back the real result. Never guess file contents or command output; read or run first. Make focused changes, \
preserve existing indentation and style, and check your work (for example run the tests) when it matters. \
Keep replies short and concrete. When the task is done, give a brief summary of what you changed."""

PROTOCOL = """[Tool protocol]
To use tools, reply with ONLY fenced code blocks tagged tool_call, one JSON object each, and nothing after them:
```tool_call
{{"name": "<tool name>", "arguments": {{<arguments as JSON>}}}}
```
Several blocks in one reply run in order. Results come back as [tool result <name>]. \
Use only these tools and argument names (? = optional):
{catalog}
When no tool is needed, answer normally without any tool_call block. The tools are connected: if the current \
message needs files or commands, reply with tool_call blocks now."""

REMINDER = ("[Reminder] To use a tool, reply with only ```tool_call blocks as described in the tool protocol; "
            "never guess file contents or command output.")

# a first answer that claims the tools are missing gets one nudge in the same conversation
REFUSAL_RE = re.compile(r"\b(can(?:no|['’])t|cannot|unable to|don['’]t have|do not have|no)\b[^.\n]{0,40}"
                        r"\b(access|use|reach|call|run)\b[^.\n]{0,60}\b(tools?|files?|workspace|repo\w*|"
                        r"file ?system|commands?|computer)\b", re.I)
REFUSAL2_RE = re.compile(r"\b(tools?|tooling|workspace)\b[^.\n]{0,40}\b(aren['’]t|are not|isn['’]t|is not|not)"
                         r"\b[^.\n]{0,20}\b(available|connected|accessible|enabled|attached)\b"
                         r"|\breconnect\b[^.\n]{0,30}\btool", re.I)
NUDGE = ("The tools are connected and working in this conversation: the coding tool runs each ```tool_call block "
         "on the user's computer and sends the real result back. Reply now with the tool_call blocks for your next "
         "step, as described in the tool protocol.")

BLOCK_RE = re.compile(r"```[ \t]*tool_call[^\n]*\n(.*?)\n?```", re.S)
XML_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
ENV_RE = re.compile(r"<env>.*?</env>", re.S)


# ---------------------------------------------------------------- tools
def function_tools(tools):
    """OpenAI `tools` -> {name: function dict}; ignores anything that is not a function tool."""
    out = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        f = t.get("function") if t.get("type", "function") == "function" else None
        f = f if isinstance(f, dict) else (t if "name" in t and "parameters" in t else None)
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]:
            out[f["name"]] = f
    return out


def _type(v):
    t = v.get("type") if isinstance(v, dict) else None
    if isinstance(t, list):
        t = "|".join(x for x in t if x != "null")
    if not t and isinstance(v, dict) and v.get("enum"):
        t = "|".join(json.dumps(x) for x in v["enum"][:6])
    return t or "any"


def signature(f):
    p = f.get("parameters") or {}
    props = p.get("properties") or {}
    req = set(p.get("required") or [])
    args = ", ".join(f"{k}{'' if k in req else '?'}: {_type(v)}" for k, v in props.items())
    desc = (f.get("description") or "").strip()
    if f["name"] in FULL_DESCRIPTION:
        return f"- {f['name']}({args}):\n" + "\n".join("    " + l for l in desc.splitlines())
    first = desc.split("\n", 1)[0]
    if len(first) > 200:
        first = first[:200].rsplit(" ", 1)[0] + "..."
    return f"- {f['name']}({args}): {first}"


class Agent:
    HOLD_CHARS = 1500     # agent-mode text shorter than this is not streamed until the answer is complete
    def __init__(self, tools, allow=DEFAULT_ALLOW, result_chars=8000):
        allf = function_tools(tools)
        if allow and "*" not in allow:
            kept = {n: f for n, f in allf.items() if n in allow}
            self.tools = kept or allf      # a client with other tool names keeps all of its tools
        else:
            self.tools = allf
        self.dropped = sorted(set(allf) - set(self.tools))
        self.result_chars = result_chars

    @classmethod
    def for_request(cls, body, cfg):
        if not getattr(cfg, "agent_mode", True):
            return None
        a = cls(body.get("tools"), getattr(cfg, "tools_allow", DEFAULT_ALLOW),
                getattr(cfg, "tool_result_chars", 8000))
        return a if a.tools else None

    def catalog(self):
        return "\n".join(signature(f) for f in self.tools.values())

    def protocol(self):
        return PROTOCOL.format(catalog=self.catalog())

    # ------------------------------------------------------------ rendering (canonical)
    @staticmethod
    def render_call(name, args):
        return "```tool_call\n" + json.dumps({"name": name, "arguments": args}, ensure_ascii=False,
                                             sort_keys=True) + "\n```"

    @classmethod
    def render_assistant(cls, text, calls):
        """calls: [(name, args dict)]."""
        parts = [text.strip()] if text and text.strip() else []
        parts += [cls.render_call(n, a) for n, a in calls]
        return "\n\n".join(parts)

    def cap(self, text):
        n = self.result_chars
        if not n or len(text) <= n:
            return text
        head, tail = int(n * 0.75), int(n * 0.25)
        omitted = len(text) - head - tail
        return (text[:head] + f"\n[... {omitted} characters omitted; use read with offset/limit, or grep, "
                f"to see the rest ...]\n" + text[-tail:])

    def system_prompt(self, systems):
        env = ""
        for s in systems:
            m = ENV_RE.search(s)
            if m:
                env = m.group(0)
        return SYSTEM + ("\n\n" + env if env else "")

    def transform(self, messages):
        """OpenAI messages (with tool_calls / tool roles) -> plain {role, content} messages.
        System messages collapse into one lean system prompt; consecutive tool/user messages
        merge into one user message."""
        systems, out, names = [], [], {}
        pending = []   # texts of the current tool/user group

        def flush():
            if pending:
                out.append({"role": "user", "content": "\n\n".join(pending)})
                pending.clear()

        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = norm_role(m.get("role"))
            text = content_text(m.get("content")).strip()
            if role == "system":
                if text:
                    systems.append(text)
                continue
            if role == "assistant":
                flush()
                calls = []
                for tc in m.get("tool_calls") or []:
                    f = (tc or {}).get("function") or {}
                    name = f.get("name") or "?"
                    args = f.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args.strip() else {}
                        except ValueError:
                            args = {"_raw": args}
                    names[tc.get("id")] = name
                    calls.append((name, args if isinstance(args, dict) else {"_value": args}))
                body = self.render_assistant(text, calls)
                if body:
                    out.append({"role": "assistant", "content": body})
                continue
            if role == "tool":
                name = names.get(m.get("tool_call_id")) or m.get("name") or "tool"
                pending.append(f"[tool result {name}]\n" + (self.cap(text) if text else "(no output)"))
                continue
            if text:
                pending.append(text)
        flush()
        if systems:
            out.insert(0, {"role": "system", "content": self.system_prompt(systems)})
        else:
            out.insert(0, {"role": "system", "content": SYSTEM})
        return out

    # ------------------------------------------------------------ prompts
    def first_prompt(self, prefix, last, budget=None):
        """First message of a new chatgpt.com conversation: instructions, (recent) conversation,
        current message, tool protocol last. With `budget`, older turns are dropped to fit, but
        the first user message (the task) is always kept."""
        system = next((m["content"] for m in prefix if m.get("role") == "system"), SYSTEM)
        turns = [m for m in prefix if m.get("role") != "system"]
        current = content_text(last.get("content")).strip()
        proto = self.protocol()
        return build_first(system, turns, current, proto, budget)

    @staticmethod
    def refused(text):
        """True when an answer without tool calls claims the tools are not available."""
        return bool(text) and len(text) < Agent.HOLD_CHARS and bool(REFUSAL_RE.search(text) or REFUSAL2_RE.search(text))

    def later_prompt(self, prompt):
        return prompt + "\n\n" + REMINDER

    # ------------------------------------------------------------ parsing
    def parse(self, answer):
        """-> (text without valid blocks, [(name, args)], [problems]). Invalid blocks stay in the text."""
        calls, problems = [], []
        spans = []
        for rx in (BLOCK_RE, XML_RE):
            for m in rx.finditer(answer or ""):
                if any(s <= m.start() < e for s, e in spans):
                    continue
                call, why = self.check(m.group(1))
                if call:
                    calls.append((m.start(), call))
                    spans.append((m.start(), m.end()))
                else:
                    problems.append(why)
        calls.sort(key=lambda x: x[0])
        text = answer or ""
        for s, e in sorted(spans, reverse=True):
            text = text[:s] + text[e:]
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, [c for _, c in calls], problems

    def check(self, raw):
        try:
            obj = json.loads(raw.strip())
        except ValueError as e:
            return None, f"not JSON ({e})"
        if not isinstance(obj, dict):
            return None, "not a JSON object"
        name = obj.get("name") or obj.get("tool")
        args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return None, f"{name}: arguments are not JSON"
        if name not in self.tools:
            return None, f"unknown tool {name!r}"
        if not isinstance(args, dict):
            return None, f"{name}: arguments must be an object"
        params = self.tools[name].get("parameters") or {}
        props = params.get("properties")
        if isinstance(props, dict) and props:
            extra = [k for k in args if k not in props]
            for k in extra:
                args.pop(k)
            if extra:
                log.info("tool %s: dropped unknown arguments %s", name, extra)
        missing = [k for k in params.get("required") or [] if k not in args]
        if missing:
            return None, f"{name}: missing required {missing}"
        return (name, args), None

    @staticmethod
    def to_openai(calls):
        return [{"id": "call_" + uuid.uuid4().hex[:24], "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in calls]


def _shorten(text, n):
    if len(text) <= n:
        return text
    head, tail = int(n * 0.7), int(n * 0.3)
    return text[:head] + f"\n[... {len(text) - head - tail} characters left out ...]\n" + text[-tail:]


def build_first(system, turns, current, protocol=None, budget=None):
    """Shared by agent and plain mode. turns: [{role, content}] (no system)."""
    label = {"user": "User", "assistant": "Assistant", "tool": "Tool result"}
    rendered = [f"{label.get(m['role'], 'User')}: {content_text(m.get('content')).strip()}" for m in turns
                if content_text(m.get("content")).strip()]
    note = ""
    if budget and rendered:
        fixed = len(system) + len(current) + len(protocol or "") + 400
        room = max(0, budget - fixed)
        first_user = next((r for r in rendered if r.startswith("User: ")), None)
        keep, size = [], len(first_user or "")
        for i, r in enumerate(reversed(rendered)):
            if r is first_user:
                continue
            if size + len(r) > room:
                if i == 0:
                    # the latest turn (usually the tool call whose result is the current
                    # message) is always kept, shortened if it does not fit
                    r = _shorten(r, max(room - size, 1500))
                    keep.insert(0, r)
                    size += len(r)
                break
            keep.insert(0, r)
            size += len(r)
        dropped = len(rendered) - len(keep) - (1 if first_user else 0)
        if dropped > 0:
            note = (f"({dropped} older messages were left out to save space; re-read files if you need "
                    f"details from them.)")
            rendered = ([first_user] if first_user else []) + [note] + keep
    parts = ["[Instructions]\n" + system]
    if rendered:
        parts.append("[Conversation so far]\n" + "\n\n".join(rendered))
    parts.append("[Current message]\n" + current)
    if protocol:
        parts.append(protocol)
    elif rendered:
        parts.append("(Continue the conversation: reply to the current message only.)")
    return "\n\n".join(parts)
