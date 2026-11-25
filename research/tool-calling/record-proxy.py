# Recording proxy: client -> 127.0.0.1:18112 -> gptapi 127.0.0.1:8000.
# Usage: python3 record-proxy.py <out-dir>; point the client baseURL at http://127.0.0.1:18112/v1.
# Saves each POST body plus gptapi's mapping headers to <out-dir>/NN.json.
import json, sys, time, http.server, urllib.request

OUT = sys.argv[1]
N = [0]


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def fwd(self, body=None):
        keep = ("content-type", "authorization", "x-conversation-id")
        req = urllib.request.Request("http://127.0.0.1:8000" + self.path, data=body, method=self.command,
                                     headers={k: v for k, v in self.headers.items() if k.lower() in keep})
        with urllib.request.urlopen(req, timeout=300) as r:
            data, hdr, status = r.read(), dict(r.headers), r.status
        self.send_response(status)
        for k, v in hdr.items():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return hdr, data

    def do_GET(self):
        self.fwd()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        N[0] += 1
        n, t = N[0], time.time()
        hdr, data = self.fwd(body)
        json.dump({"n": n, "path": self.path, "request": json.loads(body),
                   "mapping": hdr.get("X-Gptapi-Mapping"), "conversation": hdr.get("X-Conversation-Id"),
                   "seconds": round(time.time() - t, 1),
                   "response_tail": data.decode(errors="replace")[-600:]},
                  open(f"{OUT}/{n:02d}.json", "w"), indent=1)


http.server.ThreadingHTTPServer(("127.0.0.1", 18112), H).serve_forever()
