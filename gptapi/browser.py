"""chatgpt.com tabs over CDP: one private browser context, one tab per active conversation.

Blocking tab work runs in worker threads (asyncio.to_thread); the Browser class does the async
bookkeeping (per-conversation locks, LRU tab cap, reconnects).
"""
import asyncio
import base64
import collections
import json
import logging
import os
import queue
import time
import urllib.parse
import uuid

from .cdp import CDP, launch_dev_browser
from .store import TurnRecord
from .stream import StreamParser, clean_md

log = logging.getLogger("gptapi")

ORIGIN = "https://chatgpt.com"
COMPOSER = "textarea[name=prompt], #prompt-textarea, textarea"
SEND_BUTTON = 'button[aria-label="Send message"], button[data-testid="send-button"]'
# Pressing Enter right after the page loads is sometimes ignored (the page is not wired up yet):
# the prompt stays in the box and no request leaves. These read the box and the send button.
SEND_STATE_JS = """(()=>{const t=document.querySelector(%s); const b=document.querySelector(%s);
  const r=b&&b.getBoundingClientRect();
  return {text: t ? (t.value||t.innerText||'').length : 0,
          button: !!b && !b.disabled && r.width > 0, x: r ? r.x + r.width/2 : 0, y: r ? r.y + r.height/2 : 0}})()"""

HUMAN_CHECK_JS = r"""(()=>{
  const vis = e => { if(!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 40 && r.height > 40 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const frames = [...document.querySelectorAll('iframe')].filter(f => /challenges\.cloudflare\.com|turnstile/i.test(f.src||'') && vis(f));
  const title = document.title || '';
  const body = (document.body && document.body.innerText || '').slice(0, 4000);
  const dlg = [...document.querySelectorAll('[role=dialog],[role=alertdialog]')].map(e => e.innerText||'').join('\n');
  const phrase = /verify you are human|verifying you are human|are you a robot|complete the security check|confirm you('|’)re human/i;
  return {frame: frames.length > 0, interstitial: /just a moment|attention required/i.test(title),
          text: phrase.test(dlg) || phrase.test(body), title};
})()"""


class GptapiError(Exception):
    status = 502
    etype = "upstream_error"


class CapError(GptapiError):
    status = 429
    etype = "rate_limit_exceeded"


class MessageTooLong(GptapiError):
    """chatgpt.com refused the message as too large (HTTP 400). Named like OpenAI's error so
    clients that compact on context overflow can react."""
    status = 400
    etype = "context_length_exceeded"


class HumanCheckRequired(GptapiError):
    status = 503
    etype = "human_check_required"


class BrowserLost(GptapiError):
    status = 503
    etype = "browser_unavailable"


# ---------------------------------------------------------------- one tab (blocking)

class Tab:
    def __init__(self, cdp, target_id, data_dir):
        self.c, self.target_id, self.data = cdp, target_id, data_dir
        self.sid = None
        self.q = None
        self.key = None          # conversation id, or "spare-..." before one is known
        self.busy = False
        self.last_used = time.time()

    # -- setup
    def attach(self):
        self.sid = self.c.call("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})["sessionId"]
        self.q = self.c.queue(self.sid)
        self.c.call("Network.enable", {"maxTotalBufferSize": 4 << 20, "maxResourceBufferSize": 2 << 20}, self.sid)
        self.c.call("Page.enable", {}, self.sid)
        try:   # background tabs keep document.hasFocus() true
            self.c.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, self.sid)
        except Exception as e:
            log.debug("focus emulation: %s", e)
        return self

    def front(self):
        """Make this the visible tab: a hidden (background) chatgpt.com tab may not send at all."""
        try:
            self.c.call("Page.bringToFront", {}, self.sid, timeout=5)
        except Exception as e:
            log.debug("bringToFront: %s", e)

    def close(self):
        try:
            self.c.call("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        except Exception as e:
            log.debug("closeTarget: %s", e)
        if self.sid:
            self.c.drop_queue(self.sid)

    # -- page helpers
    def ev(self, expr, timeout=30):
        r = self.c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                        self.sid, timeout)
        return r.get("result", {}).get("value")

    def url(self):
        try:
            return self.ev("location.href", 5) or ""
        except Exception:
            return ""

    def human_check(self):
        try:
            h = self.ev(HUMAN_CHECK_JS, 5) or {}
        except Exception:
            return False
        return bool(h.get("frame") or h.get("interstitial") or h.get("text"))

    def send_state(self):
        try:
            return self.ev(SEND_STATE_JS % (json.dumps(COMPOSER), json.dumps(SEND_BUTTON)), 5) or {}
        except Exception:
            return {}

    def press_enter(self):
        for typ in ("keyDown", "keyUp"):
            p = {"type": typ, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
            if typ == "keyDown":
                p["text"] = "\r"
            self.c.call("Input.dispatchKeyEvent", p, self.sid)

    def click_send(self):
        """A real (trusted) mouse click on the send button. Returns False if there is none."""
        self.front()
        st = self.send_state()
        if not st.get("button"):
            return False
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.c.call("Input.dispatchMouseEvent", {"type": typ, "x": st["x"], "y": st["y"],
                                                     "button": "left", "clickCount": 1}, self.sid)
        return True

    def composer_ready(self):
        try:
            return bool(self.ev(f"!!document.querySelector({json.dumps(COMPOSER)}) && document.readyState==='complete'", 5))
        except Exception:
            return False

    def at_fresh_root(self):
        u = urllib.parse.urlparse(self.url())
        return u.netloc.endswith("chatgpt.com") and u.path in ("", "/") and self.composer_ready()

    def goto(self, url):
        self.front()
        self.c.call("Page.navigate", {"url": url}, self.sid)
        time.sleep(0.5)
        self.wait_composer(60)

    def wait_composer(self, secs=30):
        end = time.time() + secs
        human_since = None
        while time.time() < end:
            if self.c.closed.is_set():
                raise BrowserLost("lost connection to the browser")
            if self.composer_ready():
                return
            if self.human_check():
                human_since = human_since or time.time()
                if time.time() - human_since > 20:      # give the page time to pass its own check
                    raise HumanCheckRequired("human check shown on chatgpt.com")
            else:
                human_since = None
            time.sleep(0.5)
        if self.human_check():
            raise HumanCheckRequired("human check shown on chatgpt.com")
        raise GptapiError("chatgpt.com composer did not appear")

    def debug_dump(self, tag):
        d = os.path.join(self.data, "debug")
        os.makedirs(d, exist_ok=True)
        base = os.path.join(d, time.strftime("%Y%m%d-%H%M%S-") + tag)
        try:
            png = self.c.call("Page.captureScreenshot", {"format": "png"}, self.sid, timeout=10)["data"]
            with open(base + ".png", "wb") as f:
                f.write(base64.b64decode(png))
        except Exception as e:
            log.warning("screenshot failed: %s", e)
        try:
            info = self.ev("""(()=>{const d=[...document.querySelectorAll('[role=dialog],[role=alert],[role=alertdialog],[data-testid*=modal],dialog')]
                  .filter(e=>{const r=e.getBoundingClientRect(); return (e.tagName!=='DIALOG'||e.open) && r.width>0 && r.height>0})
                  .map(e=>e.innerText.trim()).filter(Boolean); return {dialogs:d, body:(document.body?document.body.innerText:'').slice(-2000),
                  url:location.href, title:document.title}})()""", 10) or {}
        except Exception as e:
            info = {"error": str(e)}
        with open(base + ".json", "w") as f:
            json.dump(info, f, indent=1, ensure_ascii=False)
        log.warning("debug dump %s.png / .json", base)
        return base, info

    def dismiss_cookie_banner(self):
        """Click "Reject non-essential" on chatgpt.com's cookie banner (the privacy-preserving
        choice). The banner can appear mid-send and swallow the typed prompt. True if clicked."""
        try:
            return bool(self.ev("""(()=>{const b=[...document.querySelectorAll('button')].find(x=>
                /^\\s*reject non-essential\\s*$/i.test(x.innerText||'') && x.getBoundingClientRect().width>0);
                if(!b) return false; b.click(); return true})()""", 5))
        except Exception:
            return False

    def type_and_send(self, text):
        self.front()
        self.wait_composer()
        if self.dismiss_cookie_banner():
            log.info("cookie banner: clicked Reject non-essential")
            time.sleep(0.5)
        ok = self.ev(f"""(()=>{{const e=document.querySelector({json.dumps(COMPOSER)}); if(!e) return false;
              e.focus(); if('value' in e && e.value){{e.select();}} return true}})()""")
        if not ok:
            raise GptapiError("composer not found")
        self.c.call("Input.insertText", {"text": text}, self.sid, timeout=60)
        # wait until the page shows a usable send button: then it is wired up and Enter works
        end = time.time() + 10
        while time.time() < end and not self.send_state().get("button"):
            time.sleep(0.25)
        time.sleep(0.2)
        self.press_enter()

    def save_cookies(self, store, ctx):
        ck = self.c.call("Storage.getCookies", {"browserContextId": ctx}).get("cookies", [])
        keep = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
        ck = [{k: c[k] for k in keep if k in c} for c in ck if "chatgpt.com" in c["domain"]]
        store.set(cookies=[c for c in ck if c.get("expires", -1) != -1 or c["name"] == "_cfuvid"])


# ---------------------------------------------------------------- one turn (blocking)

def _graft(tab, p, inject):
    """Rewrite conversationState in the paused /conversation/updates POST, then let it go."""
    req = p["request"]
    body = req.get("postData") or "".join(base64.b64decode(e.get("bytes", "")).decode()
                                          for e in req.get("postDataEntries", []))
    params = {}
    if inject and body:
        out = []
        for k, v in urllib.parse.parse_qsl(body, keep_blank_values=True):
            if k == "conversationState":
                cur = json.loads(v or "{}")
                cur.update(json.loads(inject) if isinstance(inject, str) else inject)
                v = json.dumps(cur, separators=(",", ":"))
                log.info("grafted conversationState %s", v)
            out.append((k, v))
        params["postData"] = base64.b64encode(urllib.parse.urlencode(out).encode()).decode()
    tab.c.call("Fetch.continueRequest", {"requestId": p["requestId"], **params}, tab.sid)
    tab.c.call("Fetch.disable", {}, tab.sid)


def run_turn(tab, store, ctx, prompt, conv_id, inject, on_event, raw_path=None):
    """Send one prompt in `tab` and collect the answer.

    conv_id: known chatgpt.com conversation to continue (None = new conversation).
    inject: conversationState to graft (None = the tab is live on conv_id, or new conversation).
    on_event(kind, value): "conv" (id, once known) and "text" (latest answer text) from this thread.
    """
    raw = open(raw_path, "a") if raw_path else None
    parser = StreamParser(raw)
    rec = TurnRecord(store, prompt, conv_id)
    rec.set_conv(conv_id)
    first, reason = None, None
    try:
        # page preparation
        if conv_id and not inject and conv_id not in tab.url():
            saved = (store.conv(conv_id) or {}).get("state")
            if not saved:
                raise GptapiError(f"tab lost conversation {conv_id} and no saved state to re-attach")
            inject = saved
        if inject:
            tab.goto(ORIGIN + "/")
        elif not conv_id and not tab.at_fresh_root():
            tab.goto(ORIGIN + "/")
        while True:   # drop stale events
            try:
                tab.q.get_nowait()
            except queue.Empty:
                break
        if inject:
            tab.c.call("Fetch.enable", {"patterns": [{"urlPattern": "*conversation/updates*", "requestStage": "Request"}]}, tab.sid)
        tab.type_and_send(prompt)
        t0 = time.time()
        rid, conv_sent, nudges, retyped = None, False, 0, False
        while not parser.done:
            if tab.c.closed.is_set():
                raise BrowserLost("lost connection to the browser")
            now = time.time()
            # A cookie banner can appear while sending and the prompt vanishes without a request:
            # dismiss it and type the prompt again (once).
            if rid is None and not retyped and now - t0 > 4 and not tab.send_state().get("text") \
                    and tab.dismiss_cookie_banner():
                retyped = True
                log.warning("cookie banner swallowed the prompt; clicked Reject non-essential and retyping")
                time.sleep(0.5)
                tab.type_and_send(prompt)
                t0 = now = time.time()
            # Enter ignored? The prompt is still in the box: click Send, then try Enter once more.
            if rid is None and nudges < 2 and now - t0 > 3 + 4 * nudges and tab.send_state().get("text"):
                nudges += 1
                how = "clicked Send" if nudges == 1 and tab.click_send() else "pressed Enter again"
                if how == "pressed Enter again":
                    tab.press_enter()
                log.warning("prompt still in the box %.0fs after sending; %s", now - t0, how)
            if rid is None and now - t0 > 25:
                break
            if rid and not parser.text and now - t0 > 150:
                parser.error = parser.error or "no answer after 150 s"
                break
            if now - t0 > 900:
                reason = "answer exceeded 15 minutes"
                break
            try:
                m = tab.q.get(timeout=1.0)
            except queue.Empty:
                continue
            meth, p = m.get("method"), m.get("params", {})
            changed = False
            if meth == "_closed":
                raise BrowserLost("lost connection to the browser")
            if meth == "Target.detachedFromTarget":
                raise BrowserLost("the chatgpt.com tab was closed")
            if meth == "Fetch.requestPaused":
                _graft(tab, p, inject)
                inject = None
                continue
            if meth == "Network.requestWillBeSent":
                r = p.get("request", {})
                if r.get("method") == "POST" and "/conversation/updates" in r.get("url", "") and rid is None:
                    rid = p["requestId"]
                    log.info("turn request %s", r["url"])
                    on_event("sent", rid)
                continue
            if rid is None or p.get("requestId") != rid:
                continue
            if meth == "Network.responseReceived":
                status = p["response"]["status"]
                if status != 200:
                    parser.error = f"HTTP {status}"
                r = tab.c.call("Network.streamResourceContent", {"requestId": rid}, tab.sid)
                changed = parser.feed(base64.b64decode(r.get("bufferedData", "")))
            elif meth == "Network.dataReceived" and p.get("data"):
                changed = parser.feed(base64.b64decode(p["data"]))
            elif meth in ("Network.loadingFinished", "Network.loadingFailed"):
                changed = parser.flush()
                if meth == "Network.loadingFailed" and not parser.text:
                    parser.error = parser.error or p.get("errorText")
                parser.done = True
            if parser.conv_id and not conv_sent:
                rec.set_conv(parser.conv_id)
                on_event("conv", parser.conv_id)
                conv_sent = True
            if changed:
                first = first or time.time() - t0
                on_event("text", parser.text)
                rec.snapshot(parser.message_id, parser.best)
        if rid is None and not reason:
            if tab.human_check():
                base, info = tab.debug_dump("human-check")
                rec.finish(parser, first, "human check shown")
                raise HumanCheckRequired("human check shown on chatgpt.com")
            base, info = tab.debug_dump("not-accepted")
            page = "; ".join(info.get("dialogs") or []) or (info.get("body") or "")[-600:]
            rec.finish(parser, first, "not accepted by chatgpt.com: " + page.strip()[:300])
            raise CapError("chatgpt.com did not accept the message (logged-out limit or sign-in wall). "
                           f"Page says: {page.strip()[:800]}")
        if parser.error and not reason:
            reason = parser.error
        out = rec.finish(parser, first, reason)
        if parser.error and not clean_md(parser.best):
            base, info = tab.debug_dump("turn-error")
            page = "; ".join(info.get("dialogs") or []) or (info.get("body") or "")[-600:]
            if tab.human_check():
                raise HumanCheckRequired("human check shown on chatgpt.com")
            if parser.error == "HTTP 400":
                raise MessageTooLong(f"chatgpt.com refused the message as too long ({len(prompt)} characters; "
                                     f"about 120k is accepted, 180k is not)")
            raise CapError(f"chatgpt.com turn failed ({parser.error}). Page says: {page.strip()[:800]}")
        try:
            tab.save_cookies(store, ctx)
        except Exception as e:
            log.warning("cookie save failed: %s", e)
        return {
            "conversation_id": rec.conv,
            "answer": parser.answer(),
            "conv_state": parser.conv_state,
            "message_id": parser.message_id,
            "complete": parser.complete,
            "incomplete_reason": out.get("reason") if out and out.get("incomplete") else None,
            "retracted": bool(out and out.get("retracted")),
        }
    except BaseException as e:
        if not rec.final_written:
            rec.finish(parser, first, f"{type(e).__name__}: {e}")
        if isinstance(e, GptapiError) or not isinstance(e, Exception):
            raise
        if isinstance(e, ConnectionError) or tab.c.closed.is_set():
            raise BrowserLost(f"lost connection to the browser: {e}") from e
        try:
            tab.debug_dump("exception")
        except Exception:
            pass
        raise GptapiError(f"{type(e).__name__}: {e}") from e
    finally:
        try:
            if tab.c and not tab.c.closed.is_set():
                tab.c.call("Fetch.disable", {}, tab.sid, timeout=5)
        except Exception:
            pass
        if raw:
            raw.close()


# ---------------------------------------------------------------- the browser (async bookkeeping)

class Browser:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.cdp = None
        self.ctx = None
        self.tabs = collections.OrderedDict()     # key -> Tab (LRU order, newest last)
        # chatgpt.com ignores a send from one tab while another tab of the same session is sending,
        # so sends go one at a time; the lock is held only until the request has left.
        self.send_lock = asyncio.Lock()
        self.cond = asyncio.Condition()
        self.conv_locks = {}
        self.connect_lock = asyncio.Lock()
        self.human = {"required": False, "since": None, "conversation_id": None, "message": None}
        self.launched_pid = None
        self.last_error = None

    # -- connection
    def _connect_blocking(self):
        if self.cfg.launch_browser:
            _, pid = launch_dev_browser(self.cfg.dev_port, self.cfg.dev_profile)
            self.launched_pid = pid or self.launched_pid
        cdp = CDP(self.cfg.cdp_url)
        ctxs = cdp.call("Target.getBrowserContexts").get("browserContextIds", [])
        ctx = self.store.get("context_id")
        if ctx not in ctxs:
            ctx = cdp.call("Target.createBrowserContext", {"disposeOnDetach": False})["browserContextId"]
            cookies = self.store.get("cookies") or []
            if cookies:
                try:
                    cdp.call("Storage.setCookies", {"cookies": cookies, "browserContextId": ctx})
                except Exception as e:
                    log.warning("cookie restore failed: %s", e)
            log.info("created private context %s (restored %d cookies)", ctx, len(cookies))
            self.store.set(context_id=ctx)
        return cdp, ctx

    async def ensure(self):
        if self.cdp and not self.cdp.closed.is_set():
            return
        async with self.connect_lock:
            if self.cdp and not self.cdp.closed.is_set():
                return
            if self.cdp:
                log.warning("browser connection lost; reconnecting")
                self.cdp.close()
            self.tabs.clear()
            try:
                self.cdp, self.ctx = await asyncio.to_thread(self._connect_blocking)
            except Exception as e:
                self.cdp = None
                self.last_error = str(e)
                raise BrowserLost(f"cannot connect to the browser at {self.cfg.cdp_url}: {e}")
            log.info("connected to %s at %s, context %s", self.cdp.browser, self.cfg.cdp_url, self.ctx)
            self.last_error = None

    @property
    def connected(self):
        return bool(self.cdp and not self.cdp.closed.is_set())

    # -- tabs
    def lock_for(self, conv_id):
        return self.conv_locks.setdefault(conv_id, asyncio.Lock())

    def _targets(self):
        return {t["targetId"]: t for t in self.cdp.call("Target.getTargets")["targetInfos"]}

    def _new_tab_blocking(self):
        tid = self.cdp.call("Target.createTarget", {"url": "about:blank", "browserContextId": self.ctx})["targetId"]
        return Tab(self.cdp, tid, self.cfg.data).attach()

    def _attach_existing_blocking(self, conv_id):
        """Re-use a tab from an earlier gptapi run if it is still live on this conversation."""
        tid = (self.store.conv(conv_id) or {}).get("target_id")
        if not tid:
            return None
        t = self._targets().get(tid)
        if not t or conv_id not in t.get("url", "") or t.get("browserContextId") not in (None, self.ctx):
            return None
        log.info("re-attached live tab %s for %s", tid, conv_id)
        return Tab(self.cdp, tid, self.cfg.data).attach()

    async def _make_room(self):
        """Wait until a tab slot is free, closing the least recently used idle tab if needed."""
        async with self.cond:
            while len(self.tabs) >= self.cfg.max_tabs:
                idle = [k for k, t in self.tabs.items() if not t.busy]
                if idle:
                    victim = self.tabs.pop(idle[0])
                    log.info("closing LRU tab %s", victim.key)
                    await asyncio.to_thread(victim.close)
                    break
                await self.cond.wait()

    async def _acquire(self, conv_id):
        """-> (tab, live). live=True means the tab already holds this conversation in memory."""
        await self.ensure()
        # forget tabs that were closed behind our back (user closed the window, renderer crash...)
        alive = await asyncio.to_thread(self._targets)
        for k, t in list(self.tabs.items()):
            if not t.busy and t.target_id not in alive:
                log.info("tab for %s is gone", k)
                self.tabs.pop(k, None)
                self.cdp.drop_queue(t.sid)
        if conv_id and conv_id in self.tabs and not self.tabs[conv_id].busy:
            tab = self.tabs[conv_id]
            self.tabs.move_to_end(conv_id)
            tab.busy = True
            return tab, True
        if not conv_id:
            spare = next((t for k, t in self.tabs.items() if k.startswith("spare-") and not t.busy), None)
            if spare:
                spare.busy = True
                self.tabs.move_to_end(spare.key)
                return spare, False
        await self._make_room()
        tab, live = None, False
        if conv_id:
            tab = await asyncio.to_thread(self._attach_existing_blocking, conv_id)
            live = tab is not None
        if tab is None:
            tab = await asyncio.to_thread(self._new_tab_blocking)
        tab.key = conv_id or f"spare-{uuid.uuid4().hex[:8]}"
        tab.busy = True
        self.tabs[tab.key] = tab
        return tab, live

    async def _release(self, tab, conv_id=None):
        async with self.cond:
            tab.busy = False
            tab.last_used = time.time()
            if conv_id and tab.key != conv_id:
                self.tabs.pop(tab.key, None)
                old = self.tabs.pop(conv_id, None)
                if old is not None and old is not tab:
                    await asyncio.to_thread(old.close)
                tab.key = conv_id
                self.tabs[conv_id] = tab
                self.store.update_conv(conv_id, target_id=tab.target_id)
            self.cond.notify_all()

    # -- a turn
    async def turn(self, prompt, conv_id, inject, on_event):
        """Run one turn. conv_id None = new conversation. inject = conversationState to graft
        (branching from an earlier point); otherwise the latest saved state is used if the tab
        is not live."""
        lock = self.lock_for(conv_id) if conv_id else None
        if lock:
            await lock.acquire()
        tab = None
        try:
            tab, live = await self._acquire(conv_id)
            if conv_id and not live and inject is None:
                inject = (self.store.conv(conv_id) or {}).get("state")
                if not inject:
                    raise GptapiError(f"conversation {conv_id} has no saved state; it cannot be continued")
            raw = None
            if self.cfg.raw:
                os.makedirs(os.path.join(self.cfg.data, "raw"), exist_ok=True)
                raw = os.path.join(self.cfg.data, "raw", time.strftime("%Y%m%d") + ".ndjson")
            loop = asyncio.get_running_loop()
            sending = {"held": False}

            def release_send():
                if sending["held"]:
                    sending["held"] = False
                    self.send_lock.release()

            def on_event_wrapped(kind, value):
                if kind == "sent":
                    loop.call_soon_threadsafe(release_send)
                    return
                on_event(kind, value)

            await self.send_lock.acquire()
            sending["held"] = True
            try:
                res = await asyncio.to_thread(run_turn, tab, self.store, self.ctx, prompt, conv_id, inject,
                                              on_event_wrapped, raw)
            except HumanCheckRequired as e:
                self.human.update(required=True, since=self.human.get("since") or time.time(),
                                  conversation_id=conv_id, message=str(e))
                raise
            except BrowserLost:
                if self.cdp:
                    self.cdp.close()
                raise
            finally:
                release_send()
            self.human.update(required=False, since=None, conversation_id=None, message=None)
            cid = res["conversation_id"]
            if conv_id and cid != conv_id:
                log.warning("chatgpt.com started a new conversation %s instead of continuing %s", cid, conv_id)
            await self._release(tab, cid)
            tab = None
            return res
        finally:
            if tab is not None:
                await self._release(tab)
            if lock:
                lock.release()

    # -- health
    async def health(self):
        info = {"connected": self.connected, "cdp_url": self.cfg.cdp_url,
                "browser": self.cdp.browser if self.cdp else None, "last_error": self.last_error}
        tabs = []
        if self.connected:
            for key, t in list(self.tabs.items()):
                row = {"key": key, "busy": t.busy, "target_id": t.target_id,
                       "idle_s": round(time.time() - t.last_used, 1)}
                if not t.busy:
                    try:
                        row["url"] = await asyncio.wait_for(asyncio.to_thread(t.url), 6)
                        hc = await asyncio.wait_for(asyncio.to_thread(t.human_check), 6)
                        row["human_check"] = hc
                        if hc:
                            self.human.update(required=True, since=self.human.get("since") or time.time(),
                                              conversation_id=key, message="human check visible in tab")
                    except Exception as e:
                        row["error"] = str(e)
                tabs.append(row)
            if self.human["required"] and tabs and not any(r.get("human_check") for r in tabs if not r["busy"]) \
                    and all("url" in r for r in tabs if not r["busy"]):
                self.human.update(required=False, since=None, conversation_id=None, message=None)
        return info, tabs

    def shutdown(self):
        if self.cdp:
            self.cdp.close()
