"""Pure parsing of the chatgpt.com ndjson conversation stream + stable-delta emission.

No I/O here: everything is unit-testable with synthetic ndjson lines.
"""
import json
import logging
import re

log = logging.getLogger("gptapi")

_KNOWN_CONTENT_KEYS = {
    "version", "type", "messageId", "revision", "mode", "baseRevision", "markdown", "references",
    "dil", "approvedUrls", "canonicalApprovedUrls", "deniedUrls", "status", "streamingParent",
    "conversationFollowupSuggestionsEligible",
}


def clean_md(s):
    return re.sub(r"\n{3,}", "\n\n", s or "").strip()


def split_blocks(md):
    """Split markdown on blank lines, never inside a ``` / ~~~ fence."""
    blocks, cur, fence = [], [], False
    for ln in (md or "").split("\n"):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and not ln.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


# chatgpt.com's fallbackMarkdown is re-serialized and backslash-escapes punctuation ("Paris\\!").
# Undo the escapes that are never structural; keep the ones that can change rendering.
_SAFE_ESC = re.compile(r"\\([!.()\-+=:;,?\"'%&$@^~{}/])")
_LINE_START_ESC = re.compile(r"^(\s*\d*)\\([.\-+)])")   # "1\." / "\-" at line start are real escapes
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def _unescape_text(seg):
    return _SAFE_ESC.sub(r"\1", seg)


def unescape_md(md):
    out, fence = [], False
    for ln in (md or "").split("\n"):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            out.append(ln)
            continue
        if fence or "\\" not in ln:
            out.append(ln)
            continue
        m = _LINE_START_ESC.match(ln)
        head, rest = (ln[:m.end()], ln[m.end():]) if m else ("", ln)
        parts, pos = [], 0
        for cm in _CODE_SPAN.finditer(rest):
            parts.append(_unescape_text(rest[pos:cm.start()]))
            parts.append(cm.group(0))
            pos = cm.end()
        parts.append(_unescape_text(rest[pos:]))
        out.append(head + "".join(parts))
    return "\n".join(out)


def display_blocks(md):
    return split_blocks(unescape_md(clean_md(md)))


def normalize_answer(md):
    """The canonical answer text we hand to clients (stream and non-stream agree on it)."""
    return "\n\n".join(display_blocks(md))


class StreamParser:
    """Feeds raw bytes of the ndjson stream; tracks conversation id, answer text, state, end.

    Each `content` event carries the whole answer so far in dil.fallbackMarkdown (not always a
    strict prefix of the next one), so we keep both the latest text and the fullest seen (`best`).
    """

    def __init__(self, raw_file=None):
        self.buf = b""
        self.raw = raw_file
        self.text = ""
        self.best = ""
        self.markdown_acc = ""
        self.message_id = None
        self.conv_id = None
        self.conv_state = None
        self.complete = False
        self.done = False
        self.error = None
        self.started = False

    def feed(self, data):
        """Feed bytes; returns True if the answer text changed."""
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        changed = False
        for ln in lines:
            if ln.strip():
                changed |= self.handle_line(ln)
        return changed

    def flush(self):
        changed = False
        if self.buf.strip():
            changed = self.handle_line(self.buf)
        self.buf = b""
        return changed

    def handle_line(self, line):
        if isinstance(line, str):
            line = line.encode()
        if self.raw:
            self.raw.write(line.decode("utf-8", "replace") + "\n")
        try:
            o = json.loads(line)
        except ValueError:
            log.debug("non-json stream line: %r", line[:200])
            return False
        if not isinstance(o, dict):
            return False
        t = o.get("type")
        if t == "control":
            d = o.get("detail") or {}
            kind = d.get("kind")
            if kind == "started":
                self.started = True
            elif kind == "conversation-id":
                self.conv_id = d.get("conversationId") or self.conv_id
            elif kind in ("error", "failed", "rate-limit"):
                self.error = json.dumps(d)[:500]
        elif t == "content":
            self.message_id = o.get("messageId", self.message_id)
            md = o.get("markdown") or ""
            if o.get("mode") == "replace":
                self.markdown_acc = md
            else:
                self.markdown_acc += md
            extra = set(o) - _KNOWN_CONTENT_KEYS
            if extra:
                log.info("content event with extra keys %s", sorted(extra))
            if o.get("status") == "complete":
                self.complete = True
            fb = (o.get("dil") or {}).get("fallbackMarkdown")
            if fb is not None:
                new = fb
            elif o.get("mode") == "replace":
                new = self.markdown_acc
            else:
                new = self.markdown_acc or self.text
            changed = new != self.text
            self.text = new
            if len(clean_md(new)) >= len(clean_md(self.best)):
                self.best = new
            return changed
        elif t == "state":
            self.conv_state = o.get("conversationState") or self.conv_state
        elif t == "end":
            self.done = True
        elif t == "error":
            self.error = line.decode("utf-8", "replace")[:500]
        elif t not in ("shared", "reasoning", None):
            log.debug("unhandled stream event %s", t)
        return False

    def answer(self):
        """Fullest answer seen, normalized."""
        return normalize_answer(self.best if len(clean_md(self.best)) >= len(clean_md(self.text)) else self.text)


class StableEmitter:
    """Turns an evolving (sometimes rewritten) markdown answer into append-only deltas.

    Only blocks that are followed by another block are considered finished and emitted; the
    trailing block is emitted at the end. Emitted text is never retracted, so the concatenation
    of all deltas is "\\n\\n".join(blocks) of the final text whenever the stream only appended.
    """

    def __init__(self):
        self.n = 0          # blocks emitted
        self.sent = ""      # concatenation of everything emitted

    def _emit(self, blocks):
        out = []
        for b in blocks:
            out.append(("\n\n" if self.n else "") + b)
            self.n += 1
        d = "".join(out)
        self.sent += d
        return d

    def feed(self, text):
        blocks = display_blocks(text)
        return self._emit(blocks[self.n:-1]) if len(blocks) - 1 > self.n else ""

    def finish(self, text):
        blocks = display_blocks(text)
        return self._emit(blocks[self.n:])
