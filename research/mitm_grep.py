"""Search a saved mitm flow file for where the conversation / message ids appear.
  /tmp/mitmvenv/bin/python mitm_grep.py B-flows.mitm <conversationId> <assistantMessageId>"""
import sys
from mitmproxy import io, http
path, *needles = sys.argv[1:]
needles += ["conversation_id", "backendConversationId", "parentMessageId"]
with open(path, "rb") as f:
    for fl in io.FlowReader(f).stream():
        if not isinstance(fl, http.HTTPFlow):
            continue
        parts = {"req-url": fl.request.pretty_url, "req-headers": str(fl.request.headers),
                 "req-body": fl.request.get_text(strict=False) or "",
                 "resp-headers": str(fl.response.headers) if fl.response else "",
                 "resp-body": (fl.response.get_text(strict=False) or "") if fl.response and fl.response.raw_content else ""}
        hits = sorted({f"{n[:24]}@{k}" for n in needles for k, v in parts.items() if n in v})
        if hits:
            print(fl.request.method, fl.request.pretty_url[:100], fl.response.status_code if fl.response else None, hits)
