"""Minimal Chrome DevTools Protocol client + launcher for a dedicated local Chrome."""
import itertools, json, logging, os, queue, socket, subprocess, threading, time, urllib.request

import websocket

log = logging.getLogger("gptterm")

PORT = int(os.environ.get("GPTTERM_PORT", "9333"))
PROFILE = os.path.expanduser(os.environ.get("GPTTERM_PROFILE", "~/Library/Application Support/gptterm/chrome-profile"))

# Chromium browsers, most preferred first: (bundle id, binary inside the .app)
BROWSERS = [
    ("com.google.Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("org.chromium.Chromium", "Chromium.app/Contents/MacOS/Chromium"),
    ("com.microsoft.edgemac", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("com.brave.Browser", "Brave Browser.app/Contents/MacOS/Brave Browser"),
]


def find_browser():
    """GPTTERM_BROWSER, else the usual app folders, else wherever Spotlight finds the app."""
    env = os.environ.get("GPTTERM_BROWSER")
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


def _version(port):
    try:
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2))
    except Exception:
        return None


def ensure_browser(port=PORT):
    """Reuse the gptterm Chrome if it is already listening, otherwise start it (detached, so it
    and its private tab outlive this gptterm run)."""
    v = _version(port)
    if v:
        log.info("reusing browser on :%s (%s)", port, v.get("Browser"))
        return v
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} is taken by something that is not Chrome; set GPTTERM_PORT")
    exe = find_browser()
    if not exe:
        raise RuntimeError("no Chrome/Chromium/Edge/Brave found; install Google Chrome or set GPTTERM_BROWSER")
    os.makedirs(PROFILE, exist_ok=True)
    cmd = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
           f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
           "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
           "--disable-backgrounding-occluded-windows", "--window-size=1100,800", "about:blank"]
    log.info("starting browser: %s", " ".join(cmd))
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(150):
        v = _version(port)
        if v:
            return v
        time.sleep(0.2)
    raise RuntimeError(f"{exe} did not open its debugging port {port}")


class CDP:
    """Browser-level websocket; sessions via flatten attach. Events are pushed to self.events."""

    def __init__(self, port=PORT):
        ver = _version(port)
        if not ver:
            raise ConnectionError(f"no browser on 127.0.0.1:{port}")
        self.browser = ver.get("Browser")
        self.ws = websocket.create_connection(ver["webSocketDebuggerUrl"], suppress_origin=True,
                                              timeout=None, enable_multithread=True)
        self.ids = itertools.count(1)
        self.pending = {}
        self.events = queue.Queue()
        self.closed = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                m = json.loads(self.ws.recv())
            except Exception as e:
                log.info("cdp reader stopped: %s", e)
                self.closed.set()
                for p in list(self.pending.values()):
                    p["e"].set()
                return
            if "id" in m:
                p = self.pending.get(m["id"])
                if p:
                    p["r"] = m
                    p["e"].set()
            else:
                self.events.put(m)

    def call(self, method, params=None, sid=None, timeout=30):
        i = next(self.ids)
        p = {"e": threading.Event()}
        self.pending[i] = p
        msg = {"id": i, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
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
