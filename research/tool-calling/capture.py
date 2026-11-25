# Captures an OpenAI-style request from a client (opencode) and answers "captured".
# Usage: python3 capture.py <out-dir>   (listens on 127.0.0.1:18111; point a provider baseURL at http://127.0.0.1:18111/v1)
import json, sys, time, http.server
OUT = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "cap", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        open(f"{OUT}/req-{time.time():.3f}.json", "wb").write(raw)
        req = json.loads(raw)
        chunk = lambda d, fr=None: "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "cap", "choices": [{"index": 0, "delta": d, "finish_reason": fr}]}) + "\n\n"
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write((chunk({"role": "assistant", "content": "captured"}) + chunk({}, "stop") + "data: [DONE]\n\n").encode())
        else:
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "cap", "choices": [{"index": 0, "message": {"role": "assistant", "content": "captured"}, "finish_reason": "stop"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
http.server.ThreadingHTTPServer(("127.0.0.1", 18111), H).serve_forever()
