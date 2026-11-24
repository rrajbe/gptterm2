#!/usr/bin/env python3
"""Phase 1 container test: drive a Chromium over CDP the gptterm way (private browser context,
Input.insertText + Enter, ndjson from Network.streamResourceContent) and record what happened.

Runs INSIDE the gptapi-browser container:
  python p1test.py --port 9222 --tag A --out /tmp/p1/results "msg 1" "msg 2"

Never touches a challenge: if a visible Cloudflare/Turnstile check shows up it screenshots the
page, records it and stops (a human clicks it via noVNC).
"""
import argparse, base64, json, os, queue, sys, time

os.environ.setdefault("GPTTERM_DATA", "/tmp/p1-gptterm-data")
os.makedirs(os.environ["GPTTERM_DATA"], exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cdp import CDP  # noqa: E402
from gptterm import Tab, Turn, clean_md  # noqa: E402

CHALLENGE_JS = r"""(()=>{
  const ifr=[...document.querySelectorAll('iframe')].map(f=>f.src||'').filter(s=>/challenges\.cloudflare\.com|turnstile/i.test(s));
  const vis=[...document.querySelectorAll('iframe')].filter(f=>/challenges\.cloudflare\.com/.test(f.src||'')).some(f=>{const r=f.getBoundingClientRect(); return r.width>30&&r.height>30;});
  return {title:document.title, url:location.href, turnstileIframes:ifr.length, visibleChallengeIframe:vis,
          cfInterstitial:/just a moment|verify you are human|checking your browser/i.test(document.title+' '+(document.body?document.body.innerText.slice(0,2000):'')),
          composer:!!document.querySelector('textarea[name=prompt], #prompt-textarea, textarea')};
})()"""


class LogQ(queue.Queue):
    """CDP event queue that also keeps a compact log of every network response / failure."""

    def __init__(self):
        super().__init__()
        self.net, self.urls = [], {}

    def put(self, m, *a, **k):
        meth, p = m.get("method"), m.get("params", {})
        if meth == "Network.requestWillBeSent":
            self.urls[p["requestId"]] = (p["request"]["method"], p["request"]["url"])
        elif meth == "Network.responseReceived":
            r = p["response"]
            self.net.append({"t": round(time.time(), 3), "status": r["status"], "url": r["url"][:200],
                             "method": self.urls.get(p["requestId"], ("?",))[0], "type": p.get("type")})
        elif meth == "Network.loadingFailed":
            u = self.urls.get(p["requestId"], ("?", "?"))
            if p.get("errorText") != "net::ERR_ABORTED" or "conversation" not in u[1]:
                self.net.append({"t": round(time.time(), 3), "failed": p.get("errorText"), "url": u[1][:200],
                                 "method": u[0], "blocked": p.get("blockedReason")})
        super().put(m, *a, **k)


def shot(tab, path):
    png = tab.c.call("Page.captureScreenshot", {"format": "png"}, tab.sid)["data"]
    open(path, "wb").write(base64.b64decode(png))
    return os.path.basename(path)


def one_turn(tab, prompt, raw_path, limit=180):
    turn = Turn(tab, raw_path)
    while not tab.c.events.empty():
        tab.c.events.get_nowait()
    t0 = time.time()
    tab.type_and_send(prompt)
    first = req_at = None
    while not turn.done and time.time() - t0 < limit:
        changed = turn.pump(1.0)
        if turn.rid and req_at is None:
            req_at = time.time() - t0
        if changed and first is None and turn.text.strip():
            first = time.time() - t0
        if turn.rid is None and time.time() - t0 > 30:
            turn.error = turn.error or "no /conversation/updates request within 30 s"
            break
    return {"prompt": prompt, "request_sent_s": round(req_at, 2) if req_at else None,
            "ttft_s": round(first, 2) if first else None, "total_s": round(time.time() - t0, 2),
            "http_status": turn.status, "conversation_id": turn.conv_id, "conversation_state": turn.conv_state,
            "message_id": turn.message_id, "complete": turn.complete, "error": turn.error,
            "answer": clean_md(turn.best or turn.text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--tag", default="A")
    ap.add_argument("--out", default="/tmp/p1/results")
    ap.add_argument("messages", nargs="+")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    pre = os.path.join(a.out, a.tag)
    res = {"tag": a.tag, "port": a.port, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "screenshots": [], "turns": []}

    c = CDP(a.port)
    c.events = LogQ()
    res["browser"] = c.browser
    tab = Tab(c, {})
    tab.open()
    tab.ev("1")
    res["user_agent"] = tab.ev("navigator.userAgent")

    t0 = time.time()
    c.call("Page.navigate", {"url": "https://chatgpt.com/"}, tab.sid)
    state = None
    while time.time() - t0 < 60:
        time.sleep(1)
        try:
            state = tab.ev(CHALLENGE_JS, 10)
        except Exception:
            continue
        if state and (state["composer"] or state["visibleChallengeIframe"] or state["cfInterstitial"]):
            if state["composer"] and tab.ev("document.readyState") == "complete":
                break
            if not state["composer"]:
                time.sleep(3)  # give a silent challenge a moment to clear itself
                state = tab.ev(CHALLENGE_JS, 10)
                if not state["composer"]:
                    break
    res["page_load_s"] = round(time.time() - t0, 2)
    res["page_state"] = state
    time.sleep(2)
    res["screenshots"].append(shot(tab, pre + "-01-loaded.png"))

    if not state or not state["composer"]:
        res["stopped"] = "no composer: visible challenge/interstitial or page failed -- left for a human (noVNC)"
    else:
        for i, msg in enumerate(a.messages, 1):
            r = one_turn(tab, msg, pre + "-stream.ndjson")
            r["page_url_after"] = tab.ev("location.href")
            r["page_state_after"] = tab.ev(CHALLENGE_JS, 10)
            time.sleep(1.5)
            res["screenshots"].append(shot(tab, f"{pre}-{i + 1:02d}-after-turn{i}.png"))
            res["turns"].append(r)
            print(json.dumps({k: r[k] for k in ("ttft_s", "total_s", "http_status", "conversation_id", "error")}), flush=True)
            if r["error"] and not r["answer"]:
                break
            time.sleep(4)

    # Cloudflare / sentinel related network evidence
    keys = ("challenges.cloudflare.com", "sentinel", "/conversation/", "cdn-cgi")
    res["network_interesting"] = [n for n in c.events.net if any(k in n["url"] for k in keys)]
    res["network_errors"] = [n for n in c.events.net if n.get("failed") or (n.get("status") or 0) >= 400]
    res["network_total_responses"] = len(c.events.net)
    try:
        ck = c.call("Storage.getCookies", {"browserContextId": tab.st["context_id"]}).get("cookies", [])
        res["cookie_names"] = sorted({x["name"] for x in ck if "chatgpt.com" in x["domain"]})
    except Exception as e:
        res["cookie_names"] = str(e)
    json.dump(res, open(pre + "-result.json", "w"), indent=1, ensure_ascii=False)
    print(json.dumps({"page_load_s": res["page_load_s"], "page_state": state, "stopped": res.get("stopped"),
                      "errors": res["network_errors"][:10]}, indent=1))


if __name__ == "__main__":
    main()
