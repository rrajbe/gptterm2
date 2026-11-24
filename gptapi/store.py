"""Persistent state (state.json) and streaming-safe JSONL transcripts."""
import json
import logging
import os
import threading
import time
import uuid

from .stream import clean_md

log = logging.getLogger("gptapi")

MAX_POINTS = 64   # remembered history points (hash -> conversationState) per conversation


class Store:
    """state.json layout:
    {
      "context_id": "...", "cookies": [...],
      "names": {name: conversation_id},
      "conversations": {conversation_id: {
          "state": {...latest conversationState...}, "target_id": "...", "name": "...",
          "created": ts, "updated": ts, "turns": n,
          "points": [[history_hash, conversationState, ts], ...]   # newest last
      }}
    }
    """

    def __init__(self, data_dir):
        self.data = data_dir
        self.sessions = os.path.join(data_dir, "sessions")
        self.debug = os.path.join(data_dir, "debug")
        self.inflight = os.path.join(data_dir, "inflight")
        for d in (self.sessions, self.debug, self.inflight):
            os.makedirs(d, exist_ok=True)
        self.path = os.path.join(data_dir, "state.json")
        self.lock = threading.RLock()
        try:
            with open(self.path) as f:
                self.st = json.load(f)
        except (FileNotFoundError, ValueError):
            self.st = {}
        self.st.setdefault("names", {})
        self.st.setdefault("conversations", {})

    # ---- persistence ----
    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.st, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)

    def get(self, k, default=None):
        with self.lock:
            return self.st.get(k, default)

    def set(self, **kw):
        with self.lock:
            self.st.update(kw)
            self.save()

    # ---- conversations ----
    def conv(self, cid):
        with self.lock:
            return self.st["conversations"].get(cid)

    def ensure_conv(self, cid):
        with self.lock:
            c = self.st["conversations"].setdefault(cid, {"created": time.time(), "turns": 0, "points": []})
            return c

    def update_conv(self, cid, **kw):
        with self.lock:
            c = self.ensure_conv(cid)
            c.update(kw)
            c["updated"] = time.time()
            self.save()

    def name_to_conv(self, name):
        with self.lock:
            return self.st["names"].get(name)

    def bind_name(self, name, cid):
        with self.lock:
            self.st["names"][name] = cid
            self.ensure_conv(cid)["name"] = name
            self.save()

    def names(self):
        with self.lock:
            return dict(self.st["names"])

    def add_point(self, cid, hashes, conv_state, count=True):
        """Remember that after this turn the client-visible history hashes to `hashes`."""
        with self.lock:
            c = self.ensure_conv(cid)
            pts = c.setdefault("points", [])
            conv_state = conv_state or c.get("state")
            for h in dict.fromkeys(h for h in hashes if h):
                pts.append([h, conv_state, time.time()])
            del pts[:-MAX_POINTS]
            if conv_state:
                c["state"] = conv_state
            if count:
                c["turns"] = c.get("turns", 0) + 1
            c["updated"] = time.time()
            self.save()

    def match(self, h):
        """-> (conversation_id, conversationState at that point, is_latest) or None.
        Most recently updated conversation wins on (unlikely) collisions."""
        with self.lock:
            best = None
            for cid, c in self.st["conversations"].items():
                pts = c.get("points") or []
                latest_state = c.get("state")
                for i in range(len(pts) - 1, -1, -1):
                    ph, pstate, pts_ts = pts[i]
                    if ph == h:
                        is_latest = pstate == latest_state
                        if best is None or pts_ts > best[3]:
                            best = (cid, pstate, is_latest, pts_ts)
                        break
            return best[:3] if best else None

    # ---- transcripts ----
    def append_jsonl(self, cid, rec, ts=None):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)), **rec}
        with self.lock, open(os.path.join(self.sessions, f"{cid}.jsonl"), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def recover_inflight(self):
        """A previous run died mid-answer: write what its last snapshot had."""
        for fn in os.listdir(self.inflight):
            p = os.path.join(self.inflight, fn)
            try:
                with open(p) as f:
                    snap = json.load(f)
            except (ValueError, OSError):
                os.remove(p)
                continue
            cid = snap.get("conversation_id") or "unknown-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(snap.get("t0")))
            if not snap.get("user_written"):
                self.append_jsonl(cid, {"role": "user", "conversation_id": cid, "text": snap.get("prompt", "")}, ts=snap.get("t0"))
            self.append_jsonl(cid, {"role": "assistant", "conversation_id": cid, "message_id": snap.get("message_id"),
                                    "text": clean_md(snap.get("text") or ""), "incomplete": True,
                                    "reason": "gptapi stopped mid-answer; recovered from the last 2-second snapshot"})
            os.remove(p)
            log.warning("recovered unfinished answer into sessions/%s.jsonl", cid)


class TurnRecord:
    """Owns the JSONL writes for one turn: the user line as soon as the conversation id is known,
    the assistant line exactly once however the turn ends, and a crash-recovery snapshot
    (inflight/<turn>.json, overwritten at most every 2 s) in between."""

    def __init__(self, store, prompt, conv):
        self.store, self.prompt, self.conv, self.t0 = store, prompt, conv, time.time()
        self.path = os.path.join(store.inflight, uuid.uuid4().hex + ".json")
        self.user_written = False
        self.final_written = False
        self.last_snap = 0.0
        self.snapshot(None, "", force=True)

    def set_conv(self, conv):
        if conv and not self.conv:
            self.conv = conv
        if self.conv and not self.user_written:
            self.store.append_jsonl(self.conv, {"role": "user", "conversation_id": self.conv, "text": self.prompt}, ts=self.t0)
            self.user_written = True

    def snapshot(self, message_id, text, force=False):
        if not force and time.time() - self.last_snap < 2.0:
            return
        self.last_snap = time.time()
        snap = {"conversation_id": self.conv, "prompt": self.prompt, "t0": self.t0, "user_written": self.user_written,
                "message_id": message_id, "text": text}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def finish(self, parser, first, reason=None):
        """Write the assistant line once. `reason` set => the answer is incomplete."""
        if self.final_written:
            return None
        if not self.conv:
            self.conv = "unknown-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(self.t0))
        self.set_conv(self.conv)
        final, best = clean_md(parser.text), clean_md(parser.best)
        rec = {"role": "assistant", "conversation_id": self.conv, "message_id": parser.message_id, "text": best,
               "first_token_s": round(first, 2) if first else None, "total_s": round(time.time() - self.t0, 2)}
        if best and len(final) < 0.8 * len(best):
            rec.update(retracted=True, final_text=final)
        if reason or not parser.complete:
            rec.update(incomplete=True, reason=reason or "stream ended before the answer was marked complete")
        self.store.append_jsonl(self.conv, rec)
        self.final_written = True
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        return rec
