"""Sends go one at a time across tabs: the second tab may not type until the first tab's request
has left; after that the two answers stream at the same time."""
import asyncio
import threading
import time

import gptapi.browser as b
from gptapi.config import Config


class FakeStore:
    def conv(self, cid):
        return None

    def update_conv(self, cid, **kw):
        pass


class FakeTab:
    def __init__(self, n):
        self.key, self.busy, self.target_id = f"spare-{n}", True, f"t{n}"


def test_sends_are_serialised_streams_overlap(monkeypatch):
    events, lock = [], threading.Lock()

    def log(e):
        with lock:
            events.append(e)

    def fake_run_turn(tab, store, ctx, prompt, conv_id, inject, on_event, raw=None):
        log(("type", prompt))
        time.sleep(0.2)
        on_event("sent", "rid-" + prompt)      # request left the browser
        log(("sent", prompt))
        on_event("conv", "cid-" + prompt)
        time.sleep(0.3)                        # answer streaming
        log(("end", prompt))
        return {"conversation_id": "cid-" + prompt, "answer": "x", "conv_state": None, "message_id": None,
                "complete": True, "incomplete_reason": None, "retracted": False}

    monkeypatch.setattr(b, "run_turn", fake_run_turn)
    br = b.Browser(Config(), FakeStore())
    n = iter(range(10))

    async def acquire(conv_id):
        return FakeTab(next(n)), False

    async def release(tab, conv_id=None):
        pass

    br._acquire, br._release = acquire, release
    seen = []

    async def main():
        await asyncio.gather(br.turn("A", None, None, lambda k, v: seen.append(k)),
                             br.turn("B", None, None, lambda k, v: seen.append(k)))
    asyncio.run(main())

    first = events[0][1]
    second = "B" if first == "A" else "A"
    assert events[:2] == [("type", first), ("sent", first)], events          # one sends at a time
    assert events.index(("type", second)) < events.index(("end", first)), events  # streams overlap
    assert "sent" not in seen and seen.count("conv") == 2                   # internal event not leaked
    assert not br.send_lock.locked()


def test_send_lock_released_on_error(monkeypatch):
    def boom(*a, **k):
        raise b.CapError("nope")

    monkeypatch.setattr(b, "run_turn", boom)
    br = b.Browser(Config(), FakeStore())

    async def acquire(conv_id):
        return FakeTab(0), False

    async def release(tab, conv_id=None):
        pass

    br._acquire, br._release = acquire, release

    async def main():
        try:
            await br.turn("A", None, None, lambda k, v: None)
        except b.CapError:
            pass
    asyncio.run(main())
    assert not br.send_lock.locked()
