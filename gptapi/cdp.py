"""Thread-based Chrome DevTools Protocol client (browser-level websocket, flatten sessions).

Events are routed into one queue per sessionId so several tabs can stream concurrently.
Also: optional dev-mode launcher for a local macOS Chrome with its own profile.
"""
import itertools
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import websocket

log = logging.getLogger("gptapi")

FORBIDDEN_PORTS = {9333}   # the user's own gptterm Chrome lives there — never touch it

BROWSERS = [
    ("com.google.Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("org.chromium.Chromium", "Chromium.app/Contents/MacOS/Chromium"),
    ("com.microsoft.edgemac", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("com.brave.Browser", "Brave Browser.app/Contents/MacOS/Brave Browser"),
]


def find_browser():
    """GPTAPI_BROWSER, else the usual app folders, else wherever Spotlight finds the app."""
    env = os.environ.get("GPTAPI_BROWSER")
    if env:
        return env
    for bid, rel in BROWSERS:
        for root in ("/Applications", os.path.expanduser("~/Applications")):
            p = os.path.join(root, rel)
            if os.access(p, os.X_OK):
                return p
        try:
            out = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{bid}'"],
                                 capture_output=True, text=True, timeout=10).stdout.split("\n")
        except Exception:
            out = []
        for app in filter(None, out):
            p = os.path.join(os.path.dirname(app), rel)
            if os.access(p, os.X_OK):
                return p
    return None


def _hostport(cdp_url):
    u = urllib.parse.urlparse(cdp_url if "://" in cdp_url else "http://" + cdp_url)
    return u.hostname or "127.0.0.1", u.port or 9222


def version(cdp_url, timeout=3):
    """GET /json/version. Chrome rejects Host headers that are not an IP or localhost, so a
    docker service name (http://browser:9222) is sent with Host: localhost."""
    host, port = _hostport(cdp_url)
    req = urllib.request.Request(f"http://{host}:{port}/json/version", headers={"Host": f"localhost:{port}"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception:
        return None


def launch_dev_browser(port, profile):
    """Start a local Chrome with its own profile on `port` (dev mode). Returns (version, pid|None)."""
    if port in FORBIDDEN_PORTS:
        raise RuntimeError(f"refusing to use port {port} (reserved for gptterm)")
    url = f"http://127.0.0.1:{port}"
    v = version(url)
    if v:
        log.info("reusing dev browser on :%s (%s)", port, v.get("Browser"))
        return v, None
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} is taken by something that is not Chrome")
    exe = find_browser()
    if not exe:
        raise RuntimeError("no Chrome/Chromium/Edge/Brave found; set GPTAPI_BROWSER")
    os.makedirs(profile, exist_ok=True)
    cmd = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
           f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
           "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
           "--disable-backgrounding-occluded-windows", "--window-size=1100,800", "about:blank"]
    log.info("starting dev browser: %s", " ".join(cmd))
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(150):
        v = version(url)
        if v:
            return v, p.pid
        time.sleep(0.2)
    raise RuntimeError(f"{exe} did not open its debugging port {port}")


class CDP:
    def __init__(self, cdp_url):
        self.url = cdp_url
        host, port = _hostport(cdp_url)
        if cdp_url.startswith(("ws://", "wss://")):
            ws_url, self.browser = cdp_url, "?"
        else:
            ver = version(cdp_url)
            if not ver:
                raise ConnectionError(f"no browser at {cdp_url}")
            self.browser = ver.get("Browser")
            u = urllib.parse.urlparse(ver["webSocketDebuggerUrl"])
            ws_url = u._replace(netloc=f"{host}:{port}").geturl()   # the ws URL names the browser's own view of its host
        # Chrome also checks the websocket Host header
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=None, enable_multithread=True,
                                              host=f"localhost:{port}")
        self.ids = itertools.count(1)
        self.pending = {}
        self.queues = {}            # sessionId -> queue.Queue of events
        self.qlock = threading.Lock()
        self.closed = threading.Event()
        self.send_lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True, name="cdp-reader").start()

    def _reader(self):
        while True:
            try:
                m = json.loads(self.ws.recv())
            except Exception as e:
                log.warning("cdp reader stopped: %s", e)
                self.closed.set()
                for p in list(self.pending.values()):
                    p["e"].set()
                with self.qlock:
                    for q in self.queues.values():
                        q.put({"method": "_closed"})
                return
            if "id" in m:
                p = self.pending.get(m["id"])
                if p:
                    p["r"] = m
                    p["e"].set()
                continue
            sid = m.get("sessionId")
            if m.get("method") == "Target.detachedFromTarget":
                sid = (m.get("params") or {}).get("sessionId")
            if sid:
                with self.qlock:
                    q = self.queues.get(sid)
                if q is not None:
                    q.put(m)

    def queue(self, sid):
        with self.qlock:
            return self.queues.setdefault(sid, queue.Queue())

    def drop_queue(self, sid):
        with self.qlock:
            self.queues.pop(sid, None)

    def call(self, method, params=None, sid=None, timeout=30):
        if self.closed.is_set():
            raise ConnectionError("CDP connection closed")
        i = next(self.ids)
        p = {"e": threading.Event()}
        self.pending[i] = p
        msg = {"id": i, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        try:
            with self.send_lock:
                self.ws.send(json.dumps(msg))
        except Exception as e:
            self.pending.pop(i, None)
            raise ConnectionError(f"CDP send failed: {e}")
        ok = p["e"].wait(timeout)
        self.pending.pop(i, None)
        if not ok:
            raise TimeoutError(method)
        r = p.get("r")
        if r is None:
            raise ConnectionError("CDP connection closed")
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message')}")
        return r.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
