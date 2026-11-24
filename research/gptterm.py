#!/usr/bin/env python3
"""gptterm — terminal chat with logged-out chatgpt.com, relayed through a long-lived private
tab in a dedicated local Chrome (the page solves its own Turnstile/PoW; we only type and listen).

  gptterm                 new conversation
  gptterm --resume        continue the last conversation
  gptterm --resume ID     continue conversation ID
  gptterm --list          list saved conversations
  gptterm --check         start Chrome, load chatgpt.com, report whether it is ready
  gptterm --quit-browser  close the dedicated Chrome
"""
import argparse, base64, queue, urllib.parse, fcntl, json, logging, os, re, readline, signal, sys, time  # noqa: F401 (readline: line editing)

from cdp import CDP, PORT, PROFILE, ensure_browser

__version__ = "0.0.1"

HOME = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser(os.environ.get("GPTTERM_DATA", os.path.join(HOME, "data")))
SESS = os.path.join(DATA, "sessions")
STATE = os.path.join(DATA, "state.json")
ORIGIN = "https://chatgpt.com"
COMPOSER = "textarea[name=prompt], #prompt-textarea, textarea"
log = logging.getLogger("gptterm")


# ---------- state / transcript ----------

def load_state():
    try:
        return json.load(open(STATE))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def append_jsonl(conv_id, rec, ts=None):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)), **rec}
    with open(os.path.join(SESS, f"{conv_id}.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def list_sessions():
    rows = []
    for fn in os.listdir(SESS):
        if not fn.endswith(".jsonl"):
            continue
        p = os.path.join(SESS, fn)
        recs = [json.loads(l) for l in open(p) if l.strip()]
        users = [r for r in recs if r.get("role") == "user"]
        rows.append((os.path.getmtime(p), fn[:-6], len(users), users[0]["text"] if users else ""))
    for mt, cid, n, first in sorted(rows, reverse=True):
        print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))}  {cid}  {n:3d} turns  {first[:60]!r}")


# ---------- the browser tab ----------

class Tab:
    def __init__(self, cdp, st):
        self.c, self.st = cdp, st
        self.sid = None
        self.pending_state = None

    def ev(self, expr, timeout=30):
        r = self.c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                        self.sid, timeout)
        return r.get("result", {}).get("value")

    def open(self):
        """Attach to our long-lived private tab, recreating it (and its cookies) if Chrome lost it."""
        infos = {t["targetId"]: t for t in self.c.call("Target.getTargets")["targetInfos"]}
        tid = self.st.get("target_id")
        created = False
        if tid not in infos:
            ctx = self.st.get("context_id")
            ctxs = self.c.call("Target.getBrowserContexts").get("browserContextIds", [])
            if ctx not in ctxs:
                ctx = self.c.call("Target.createBrowserContext", {"disposeOnDetach": False})["browserContextId"]
                if self.st.get("cookies"):
                    self.c.call("Storage.setCookies", {"cookies": self.st["cookies"], "browserContextId": ctx})
                log.info("created private context %s (restored %d cookies)", ctx, len(self.st.get("cookies") or []))
            tid = self.c.call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
            self.st.update(context_id=ctx, target_id=tid)
            save_state(self.st)
            created = True
        self.sid = self.c.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        self.c.call("Network.enable", {"maxTotalBufferSize": 4 << 20, "maxResourceBufferSize": 2 << 20}, self.sid)
        self.c.call("Page.enable", {}, self.sid)
        return created

    def url(self):
        return self.ev("location.href") or ""

    def goto(self, url):
        self.c.call("Page.navigate", {"url": url}, self.sid)
        self.wait_composer(45)

    def wait_composer(self, secs=30):
        end = time.time() + secs
        while time.time() < end:
            try:
                if self.ev(f"!!document.querySelector({json.dumps(COMPOSER)}) && document.readyState==='complete'"):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("chatgpt.com composer did not appear (see data/debug)")

    def save_cookies(self):
        ck = self.c.call("Storage.getCookies", {"browserContextId": self.st["context_id"]}).get("cookies", [])
        keep = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
        self.st["cookies"] = [{k: c[k] for k in keep if k in c} for c in ck if "chatgpt.com" in c["domain"]]
        self.st["cookies"] = [c for c in self.st["cookies"] if c.get("expires", -1) != -1 or c["name"] in ("_cfuvid",)]

    def debug_dump(self, tag):
        os.makedirs(os.path.join(DATA, "debug"), exist_ok=True)
        base = os.path.join(DATA, "debug", time.strftime("%Y%m%d-%H%M%S-") + tag)
        try:
            png = self.c.call("Page.captureScreenshot", {"format": "png"}, self.sid)["data"]
            open(base + ".png", "wb").write(base64.b64decode(png))
        except Exception as e:
            log.warning("screenshot failed: %s", e)
        text = self.ev("""(()=>{const d=[...document.querySelectorAll('[role=dialog],[role=alert],[data-testid*=modal]')]
              .map(e=>e.innerText.trim()).filter(Boolean); return {dialogs:d, body:document.body.innerText.slice(-1500), url:location.href}})()""") or {}
        json.dump(text, open(base + ".json", "w"), indent=1)
        return base, text

    def type_and_send(self, text):
        self.wait_composer()
        ok = self.ev(f"""(()=>{{const e=document.querySelector({json.dumps(COMPOSER)}); if(!e) return false;
              e.focus(); if('value' in e && e.value){{e.select();}} return true}})()""")
        if not ok:
            raise RuntimeError("composer not found")
        self.c.call("Input.insertText", {"text": text}, self.sid)
        time.sleep(0.15)
        for typ in ("keyDown", "keyUp"):
            p = {"type": typ, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
            if typ == "keyDown":
                p["text"] = "\r"
            self.c.call("Input.dispatchKeyEvent", p, self.sid)

    def stop_generation(self):
        self.ev("""(()=>{const b=document.querySelector('[data-testid=stop-button],button[aria-label*="Stop" i]'); b&&b.click(); return !!b})()""")


# ---------- streaming one turn ----------

class Turn:
    """Collects the ndjson conversation stream for one prompt from CDP Network events."""

    def __init__(self, tab, raw_path=None):
        self.tab, self.raw = tab, (open(raw_path, "a") if raw_path else None)
        self.rid, self.buf = None, b""
        self.text, self.message_id, self.conv_id = "", None, None
        self.markdown_acc = ""
        self.done = False
        self.error = None
        self.status = None
        self.conv_state = None
        self.best = ""          # fullest answer seen while streaming (kept even if chatgpt.com later retracts it)
        self.complete = False   # a content event said status=complete
        self.inject = tab.pending_state  # saved conversationState to graft onto the first request

    def _lines(self, data):
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for ln in lines:
            if ln.strip():
                yield ln

    def _handle(self, line):
        if self.raw:
            self.raw.write(line.decode("utf-8", "replace") + "\n")
        try:
            o = json.loads(line)
        except ValueError:
            log.debug("non-json stream line: %r", line[:200])
            return False
        t = o.get("type")
        if t == "control":
            d = o.get("detail", {})
            if d.get("kind") == "conversation-id":
                self.conv_id = d.get("conversationId")
            elif d.get("kind") in ("error", "failed", "rate-limit"):
                self.error = json.dumps(d)[:500]
            log.debug("control %s", d.get("kind"))
        elif t == "content":
            self.message_id = o.get("messageId", self.message_id)
            md = o.get("markdown") or ""
            if o.get("mode") == "replace":
                self.markdown_acc = md
            else:
                self.markdown_acc += md
            fb = (o.get("dil") or {}).get("fallbackMarkdown")
            extra = set(o) - {"version", "type", "messageId", "revision", "mode", "baseRevision", "markdown", "references",
                              "dil", "approvedUrls", "canonicalApprovedUrls", "deniedUrls", "status", "streamingParent",
                              "conversationFollowupSuggestionsEligible"}
            if extra:
                log.info("content event with extra keys %s: %s", sorted(extra), line[:400])
            if o.get("status") == "complete":
                self.complete = True
            if fb is not None:
                new = fb
            elif o.get("mode") == "replace":
                new = self.markdown_acc      # a bare replace can shrink or blank the answer
            else:
                new = self.markdown_acc or self.text
            changed = new != self.text
            self.text = new
            if len(clean_md(new)) >= len(clean_md(self.best)):
                self.best = new
            return changed
        elif t == "reasoning":
            log.debug("reasoning event")
        elif t == "state":
            self.conv_state = o.get("conversationState")
        elif t == "end":
            self.done = True
        elif t == "error" or (t not in ("shared", "state") and t):
            log.warning("unhandled stream event %s: %s", t, line[:300])
            if t == "error":
                self.error = line.decode("utf-8", "replace")[:500]
        return False

    def _graft(self, p):
        """Rewrite conversationState in the paused /conversation/updates POST, then let it go."""
        c, sid = self.tab.c, self.tab.sid
        req = p["request"]
        body = req.get("postData") or "".join(base64.b64decode(e.get("bytes", "")).decode() for e in req.get("postDataEntries", []))
        params = {}
        if self.inject and body:
            q = urllib.parse.parse_qsl(body, keep_blank_values=True)
            out = []
            for k, v in q:
                if k == "conversationState":
                    cur = json.loads(v or "{}")
                    cur.update(json.loads(self.inject))
                    v = json.dumps(cur, separators=(",", ":"))
                    log.info("grafted conversationState %s", v)
                out.append((k, v))
            params["postData"] = base64.b64encode(urllib.parse.urlencode(out).encode()).decode()
        c.call("Fetch.continueRequest", {"requestId": p["requestId"], **params}, sid)
        c.call("Fetch.disable", {}, sid)
        self.tab.pending_state = self.inject = None

    def pump(self, timeout):
        """Process CDP events for up to `timeout` s. Returns True if the answer text changed."""
        c, sid = self.tab.c, self.tab.sid
        changed = False
        end = time.time() + timeout
        while time.time() < end and not self.done:
            try:
                m = c.events.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            if m.get("sessionId") != sid:
                continue
            p, meth = m.get("params", {}), m.get("method")
            if meth == "Fetch.requestPaused":
                self._graft(p)
                continue
            if meth == "Network.requestWillBeSent":
                u = p["request"]["url"]
                if p["request"]["method"] == "POST" and "/conversation/updates" in u and self.rid is None:
                    self.rid = p["requestId"]
                    log.info("turn request %s", u)
            elif p.get("requestId") != self.rid or self.rid is None:
                continue
            elif meth == "Network.responseReceived":
                self.status = p["response"]["status"]
                if self.status != 200:
                    self.error = f"HTTP {self.status}"
                r = c.call("Network.streamResourceContent", {"requestId": self.rid}, sid)
                for ln in self._lines(base64.b64decode(r.get("bufferedData", ""))):
                    changed |= self._handle(ln)
            elif meth == "Network.dataReceived" and p.get("data"):
                for ln in self._lines(base64.b64decode(p["data"])):
                    changed |= self._handle(ln)
            elif meth in ("Network.loadingFinished", "Network.loadingFailed"):
                if self.buf.strip():
                    changed |= self._handle(self.buf)
                    self.buf = b""
                if meth == "Network.loadingFailed" and not self.text:
                    self.error = self.error or p.get("errorText")
                self.done = True
            if changed:
                break
        return changed


# ---------- rendering ----------

def clean_md(s):
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def split_blocks(md):
    """Split markdown on blank lines, never inside a ``` fence."""
    blocks, cur, fence = [], [], False
    for ln in md.split("\n"):
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


class Renderer:
    """Rich: finished blocks are printed for good; only the block still being written is live."""

    def __init__(self, plain):
        self.plain = plain
        if not plain:
            from rich.console import Console
            from rich.live import Live
            from rich.markdown import Markdown
            self.Console, self.Live, self.Markdown = Console, Live, Markdown
            self.console = Console(highlight=False)

    def user_prompt(self):
        if self.plain:
            return input("\nyou> ")
        return self.console.input("\n[bold cyan]you›[/] ")

    def info(self, msg, style="dim"):
        if self.plain:
            print(msg, file=sys.stderr)
        else:
            self.console.print(msg, style=style, markup=False, highlight=False)

    def start(self):
        self.printed = 0
        self.plain_len = 0
        if not self.plain:
            self.console.print("[bold green]gpt›[/]")
            self.live = self.Live(self.Markdown(""), console=self.console, refresh_per_second=12,
                                  transient=True, vertical_overflow="crop")
            self.live.start()

    def update(self, text, final=False):
        text = clean_md(text)
        if self.plain:
            if text[:self.plain_len] == getattr(self, "_last", "")[:self.plain_len]:
                sys.stdout.write(text[self.plain_len:])
            else:
                sys.stdout.write("\n" + text)
            sys.stdout.flush()
            self._last, self.plain_len = text, len(text)
            return
        blocks = split_blocks(text)
        stable = blocks if final else blocks[:-1]
        for b in stable[self.printed:]:
            self.live.console.print(self._block(b))
            self.live.console.print()
        self.printed = max(self.printed, len(stable))
        self.live.update(self.Markdown("" if final else (blocks[-1] if blocks else "")))

    def _block(self, md):
        """Render one markdown block with rich, trimming rich's own blank padding lines."""
        import io
        from rich.text import Text
        buf = io.StringIO()
        off = self.Console(file=buf, force_terminal=True, color_system=self.console.color_system,
                           width=self.console.width, highlight=False)
        off.print(self.Markdown(md))
        lines = buf.getvalue().split("\n")
        blank = lambda l: not re.sub(r"\x1b\[[0-9;]*m", "", l).strip()
        while lines and blank(lines[0]):
            lines.pop(0)
        while lines and blank(lines[-1]):
            lines.pop()
        return Text.from_ansi("\n".join(lines))

    def finish(self, text):
        if self.plain:
            print()
            return
        self.update(text, final=True)
        self.live.stop()


# ---------- app ----------

class CapError(Exception):
    pass


class Terminated(BaseException):
    """Raised from SIGHUP/SIGTERM so the current turn is saved before exiting."""


def _on_signal(signum, frame):
    raise Terminated(signal.Signals(signum).name)


INFLIGHT = os.path.join(DATA, "inflight.json")


class TurnRecord:
    """Owns the JSONL writes for one turn: the user line as soon as the conversation id is known,
    the assistant line exactly once however the turn ends, and a crash-recovery snapshot
    (data/inflight.json, overwritten every 2 s) in between."""

    def __init__(self, prompt, conv):
        self.prompt, self.conv, self.t0 = prompt, conv, time.time()
        self.user_written = False
        self.final_written = False
        self.last_snap = 0.0
        self.snapshot({"text": "", "message_id": None}, force=True)

    def set_conv(self, conv):
        if conv and not self.conv:
            self.conv = conv
        if self.conv and not self.user_written:
            append_jsonl(self.conv, {"role": "user", "conversation_id": self.conv, "text": self.prompt}, ts=self.t0)
            self.user_written = True

    def snapshot(self, turn, force=False):
        if not force and time.time() - self.last_snap < 2.0:
            return
        self.last_snap = time.time()
        snap = {"conversation_id": self.conv, "prompt": self.prompt, "t0": self.t0, "user_written": self.user_written,
                "message_id": turn["message_id"] if isinstance(turn, dict) else turn.message_id,
                "text": turn["text"] if isinstance(turn, dict) else turn.best}
        tmp = INFLIGHT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, INFLIGHT)

    def finish(self, turn, first, reason=None):
        """Write the assistant line once. `reason` set => the answer is incomplete."""
        if self.final_written:
            return None
        if not self.conv:
            self.conv = "unknown-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(self.t0))
        self.set_conv(self.conv)
        final, best = clean_md(turn.text), clean_md(turn.best)
        rec = {"role": "assistant", "conversation_id": self.conv, "message_id": turn.message_id, "text": best,
               "first_token_s": round(first, 2) if first else None, "total_s": round(time.time() - self.t0, 2)}
        retracted = bool(best) and len(final) < 0.8 * len(best)
        if retracted:
            rec.update(retracted=True, final_text=final)
        if reason or not turn.complete:
            rec.update(incomplete=True, reason=reason or "stream ended before the answer was marked complete")
        append_jsonl(self.conv, rec)
        self.final_written = True
        try:
            os.remove(INFLIGHT)
        except FileNotFoundError:
            pass
        return rec


def recover_inflight(ui):
    """A previous gptterm died mid-answer (kill -9, crash, power loss): save what it had."""
    try:
        snap = json.load(open(INFLIGHT))
    except (FileNotFoundError, ValueError):
        return
    conv = snap.get("conversation_id") or "unknown-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(snap.get("t0")))
    if not snap.get("user_written"):
        append_jsonl(conv, {"role": "user", "conversation_id": conv, "text": snap.get("prompt", "")}, ts=snap.get("t0"))
    append_jsonl(conv, {"role": "assistant", "conversation_id": conv, "message_id": snap.get("message_id"),
                        "text": clean_md(snap.get("text") or ""), "incomplete": True,
                        "reason": "gptterm stopped mid-answer; recovered from the last 2-second snapshot"})
    os.remove(INFLIGHT)
    ui.info(f"recovered an unfinished answer from the last run into data/sessions/{conv}.jsonl", "yellow")


def run_turn(tab, st, prompt, ui, raw_path):
    turn = Turn(tab, raw_path)
    rec = TurnRecord(prompt, st.get("conversation_id"))
    rec.set_conv(rec.conv)          # known conversation: user line goes down right away
    # drop stale events so we only see this turn's request
    while not tab.c.events.empty():
        tab.c.events.get_nowait()
    if turn.inject:
        tab.c.call("Fetch.enable", {"patterns": [{"urlPattern": "*conversation/updates*", "requestStage": "Request"}]}, tab.sid)
    first, reason, fatal = None, None, None
    try:
        tab.type_and_send(prompt)
        ui.start()
        while not turn.done:
            if tab.c.closed.is_set():
                raise ConnectionError("lost connection to Chrome")
            changed = turn.pump(1.0)
            if turn.conv_id:
                rec.set_conv(turn.conv_id)
            if turn.rid is None and time.time() - rec.t0 > 25:
                break
            if turn.rid and not turn.text and time.time() - rec.t0 > 120:
                turn.error = turn.error or "no answer after 120 s"
                break
            if changed:
                first = first or time.time() - rec.t0
                ui.update(turn.text)
                rec.snapshot(turn)
    except KeyboardInterrupt:
        reason = "interrupted (Ctrl-C)"
        try:
            tab.stop_generation()
            turn.pump(2.0)
        except Exception:
            pass
    except Terminated as e:
        reason, fatal = f"gptterm terminated ({e})", e
    except Exception as e:
        reason, fatal = f"{type(e).__name__}: {e}", e
    finally:
        try:
            ui.finish(turn.best if len(clean_md(turn.best)) > len(clean_md(turn.text)) else turn.text)
        except Exception:
            pass
    if turn.rid is None and not fatal and not reason:
        base, info = tab.debug_dump("no-request")
        dialogs = "; ".join(info.get("dialogs") or []) or info.get("body", "")[-400:]
        rec.finish(turn, first, "not accepted by chatgpt.com: " + dialogs.strip()[:300])
        raise CapError(f"ChatGPT did not accept the message (likely the logged-out limit or a sign-in wall).\n"
                       f"Page says: {dialogs.strip()}\nScreenshot: {base}.png")
    if turn.error and not reason:
        reason = turn.error
    out = rec.finish(turn, first, reason)
    if fatal:
        raise fatal
    if turn.conv_id and st.get("conversation_id") and turn.conv_id != st["conversation_id"]:
        ui.info(f"[warning: chatgpt.com started a new conversation {turn.conv_id}; earlier context is gone]", "yellow")
    st["conversation_id"] = rec.conv
    if turn.conv_state:
        st.setdefault("conv_states", {})[rec.conv] = turn.conv_state
    try:
        tab.save_cookies()
    except Exception as e:
        log.warning("cookie save failed: %s", e)
    save_state(st)
    if out and out.get("retracted"):
        ui.info("[chatgpt.com removed or shortened this answer after streaming it; the full streamed text is saved]", "yellow")
    if out and out.get("incomplete"):
        ui.info(f"[incomplete: {out['reason']}; partial answer saved]", "yellow")
    if turn.error and not turn.text:
        base, info = tab.debug_dump("turn-error")
        raise CapError(f"turn failed: {turn.error}\nPage says: {'; '.join(info.get('dialogs') or [])}\nScreenshot: {base}.png")
    return rec.conv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", nargs="?", const="last", metavar="ID", help="continue the last (or given) conversation")
    ap.add_argument("--list", action="store_true", help="list saved conversations")
    ap.add_argument("--plain", action="store_true", help="raw markdown, no colours")
    ap.add_argument("--raw", action="store_true", help="also save raw ndjson stream to data/raw/")
    ap.add_argument("--check", action="store_true", help="start Chrome, open chatgpt.com, report readiness")
    ap.add_argument("--quit-browser", action="store_true", help="close the dedicated Chrome")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"gptterm {__version__}")
    a = ap.parse_args()
    os.makedirs(SESS, exist_ok=True)
    logging.basicConfig(filename=os.path.join(DATA, "gptterm.log"), level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.list:
        return list_sessions()

    lock = open(os.path.join(DATA, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another gptterm is running (one tab, one conversation at a time)")

    ui = Renderer(a.plain or not sys.stdout.isatty())
    signal.signal(signal.SIGHUP, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    recover_inflight(ui)
    st = load_state()
    if a.quit_browser:
        try:
            CDP().call("Browser.close")
            print("gptterm Chrome closed")
        except ConnectionError:
            print("gptterm Chrome is not running")
        return 0
    ui.info("starting/attaching the gptterm Chrome…")
    try:
        ensure_browser()
    except RuntimeError as e:
        ui.info(str(e), "bold red")
        return 4
    cdp = CDP()
    try:
        tab = Tab(cdp, st)
        tab.open()
        if a.check:
            if ORIGIN not in tab.url():
                tab.goto(ORIGIN + "/")
            else:
                tab.wait_composer()
            print(f"OK  browser={cdp.browser}  port={PORT}\n    profile={PROFILE}\n    chatgpt.com composer ready; data in {DATA}")
            return 0
        if a.resume:
            conv = st.get("conversation_id") if a.resume == "last" else a.resume
            if not conv:
                sys.exit("nothing to resume yet")
            target = f"{ORIGIN}/uc/{conv}"
            if conv in tab.url():
                tab.wait_composer()
                ui.info(f"resumed {conv} (live tab)")
            elif (st.get("conv_states") or {}).get(conv):
                # anonymous chats can't be reloaded by URL; graft the saved state onto the next request
                tab.goto(ORIGIN + "/")
                tab.pending_state = st["conv_states"][conv]
                ui.info(f"resumed {conv} (tab was reset; reattaching from saved state)")
            else:
                ui.info(f"no saved state for {conv}; chatgpt.com can't reopen it. "
                        f"Its transcript is in data/sessions/{conv}.jsonl", "yellow")
                return 2
            st["conversation_id"] = conv
        else:
            tab.goto(ORIGIN + "/")
            st["conversation_id"] = None
            ui.info("new conversation")
        save_state(st)
        ui.info("type a message; end a line with \\ to continue it; /new, /id, /quit")
        raw_dir = os.path.join(DATA, "raw")
        if a.raw:
            os.makedirs(raw_dir, exist_ok=True)
        while True:
            try:
                lines = [ui.user_prompt()]
                while lines[-1].endswith("\\"):
                    lines[-1] = lines[-1][:-1]
                    lines.append(input("… "))
            except (EOFError, KeyboardInterrupt, Terminated):
                print()
                break
            prompt = "\n".join(lines).strip()
            if not prompt:
                continue
            if prompt in ("/quit", "/exit", "/q"):
                break
            if prompt == "/id":
                ui.info(f"conversation {st.get('conversation_id') or '(none yet)'}  → data/sessions/")
                continue
            if prompt == "/new":
                tab.goto(ORIGIN + "/")
                tab.pending_state = None
                st["conversation_id"] = None
                save_state(st)
                ui.info("new conversation")
                continue
            raw = os.path.join(raw_dir, time.strftime("%Y%m%d") + ".ndjson") if a.raw else None
            try:
                run_turn(tab, st, prompt, ui, raw)
            except CapError as e:
                ui.info(str(e), "bold red")
                ui.info("Stopping. Your transcript so far is saved in data/sessions/.", "red")
                return 3
            except Terminated:
                return 5
            except Exception as e:
                log.exception("turn crashed")
                ui.info(f"{type(e).__name__}: {e}\nThe answer so far is saved. Run `gptterm --resume` to carry on.", "bold red")
                return 6
    finally:
        cdp.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
