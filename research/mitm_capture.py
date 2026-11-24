"""mitmdump addon for Phase 1 test B (test-only, never used by the product).

  mitmdump -s mitm_capture.py --listen-host 127.0.0.1 -p 8080 -w B-flows.mitm --set capdir=/tmp/p1/results

- Streams the ndjson /conversation/updates response straight through (so the page still sees it
  live) while teeing every chunk to B-mitm-stream.ndjson.
- Writes B-mitm-requests.jsonl: one line per chatgpt.com / cloudflare request with status,
  and for /conversation/updates the decoded form fields (tokens truncated).
"""
import json, os, time, urllib.parse

from mitmproxy import ctx, http

TOKEN_FIELDS = ("chatRequirementsToken", "proofToken", "turnstileToken", "timingToken", "conduit")


class Capture:
    def load(self, loader):
        loader.add_option("capdir", str, "/tmp/p1/results", "output dir")

    def _w(self, name, obj):
        with open(os.path.join(ctx.options.capdir, name), "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def responseheaders(self, flow: http.HTTPFlow):
        if "/conversation/updates" in flow.request.path:
            path = os.path.join(ctx.options.capdir, "B-mitm-stream.ndjson")

            def tee(chunk: bytes) -> bytes:
                with open(path, "ab") as f:
                    f.write(chunk)
                return chunk
            flow.response.stream = tee

    def response(self, flow: http.HTTPFlow):
        h = flow.request.pretty_host
        if not any(k in h for k in ("chatgpt.com", "openai.com", "oaistatic", "cloudflare")):
            return
        rec = {"t": round(time.time(), 3), "method": flow.request.method, "url": flow.request.pretty_url[:220],
               "status": flow.response.status_code, "http_version": flow.request.http_version,
               "cf_mitigated": flow.response.headers.get("cf-mitigated"),
               "server": flow.response.headers.get("server"),
               "ctype": flow.response.headers.get("content-type", "")[:60]}
        if "/conversation/updates" in flow.request.path:
            form = dict(urllib.parse.parse_qsl(flow.request.get_text() or "", keep_blank_values=True))
            rec["form"] = {k: (v[:40] + f"...({len(v)} chars)" if any(t in k for t in TOKEN_FIELDS) and len(v) > 40 else v)
                           for k, v in form.items()}
            rec["req_headers"] = {k: v for k, v in flow.request.headers.items()
                                  if k.lower().startswith(("x-", "oai-")) and "token" not in k.lower()}
        if "/conversation/prepare" in flow.request.path or "/chat-requirements/" in flow.request.path:
            try:
                rec["resp_keys"] = sorted(json.loads(flow.response.get_text() or "{}"))
            except Exception:
                pass
        self._w("B-mitm-requests.jsonl", rec)


addons = [Capture()]
