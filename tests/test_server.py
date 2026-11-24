"""Server tests with a fake browser (no Chrome, no network)."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from gptapi.browser import CapError, HumanCheckRequired
from gptapi.config import Config
from gptapi.server import APP_KEY, build_app

CID = "6a1f0c2e-1111-4222-8333-944455556666"


class FakeBrowser:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []
        self.human = {"required": False, "since": None, "conversation_id": None, "message": None}
        self.connected = True

    async def ensure(self):
        pass

    async def health(self):
        return {"connected": True}, []

    def shutdown(self):
        pass

    async def turn(self, prompt, conv_id, inject, on_event):
        self.calls.append((prompt, conv_id, inject))
        if self.fail:
            raise self.fail
        on_event("conv", CID)
        for t in ("Hi", "Hi there.\n\nSec", "Hi there.\n\nSecond block."):
            on_event("text", t)
            await asyncio.sleep(0.01)
        return {"conversation_id": CID, "answer": "Hi there.\n\nSecond block.", "message_id": "m",
                "conv_state": {"backendConversationId": CID, "parentMessageId": f"p{len(self.calls)}"},
                "complete": True, "incomplete_reason": None, "retracted": False}


def run(coro):
    return asyncio.run(coro)


async def _client(tmp_path, fake, key=""):
    cfg = Config(data=str(tmp_path), api_key=key)
    app = build_app(cfg)
    app[APP_KEY].browser = fake
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def test_nonstream_then_history_maps(tmp_path):
    async def go():
        fake = FakeBrowser()
        c = await _client(tmp_path, fake)
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}]
        r = await c.post("/v1/chat/completions", json={"model": "gpt-x", "messages": msgs, "tools": [{"x": 1}]})
        j = await r.json()
        assert r.status == 200 and r.headers["X-Conversation-Id"] == CID
        assert j["id"].endswith(CID) and j["choices"][0]["message"]["content"] == "Hi there.\n\nSecond block."
        msgs += [{"role": "assistant", "content": j["choices"][0]["message"]["content"]}, {"role": "user", "content": "more"}]
        r = await c.post("/v1/chat/completions", json={"model": "gpt-x", "messages": msgs})
        assert r.headers["X-Gptapi-Mapping"] == "matched"
        assert fake.calls[1] == ("more", CID, None)
        r = await c.get("/v1/models")
        assert [m["id"] for m in (await r.json())["data"]] == ["chatgpt-web"]
        await c.close()
    run(go())


def test_stream_chunks(tmp_path):
    async def go():
        c = await _client(tmp_path, FakeBrowser())
        r = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}], "stream": True,
                                                        "stream_options": {"include_usage": True}})
        body = await r.text()
        events = [l[6:] for l in body.split("\n") if l.startswith("data: ")]
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks if ch["choices"])
        assert text == "Hi there.\n\nSecond block."
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert any(ch["choices"] and ch["choices"][0]["finish_reason"] == "stop" for ch in chunks)
        assert chunks[-1]["usage"]["total_tokens"] > 0
        assert r.headers["X-Conversation-Id"] == CID
        await c.close()
    run(go())


def test_named_model(tmp_path):
    async def go():
        fake = FakeBrowser()
        c = await _client(tmp_path, fake)
        await c.post("/v1/chat/completions", json={"model": "chatgpt-web/work", "messages": [{"role": "user", "content": "a"}]})
        r = await c.post("/v1/chat/completions", json={"model": "chatgpt-web/work", "messages": [{"role": "user", "content": "b"}]})
        assert r.headers["X-Gptapi-Mapping"] == "named" and fake.calls[1][1] == CID
        ids = [m["id"] for m in (await (await c.get("/v1/models")).json())["data"]]
        assert "chatgpt-web/work" in ids
        await c.close()
    run(go())


def test_errors_and_auth(tmp_path):
    async def go():
        c = await _client(tmp_path, FakeBrowser(HumanCheckRequired("x")))
        r = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}], "stream": True})
        j = await r.json()
        assert r.status == 503 and j["error"]["type"] == "human_check_required" and "6080" in j["error"]["message"]
        await c.close()
        c = await _client(tmp_path, FakeBrowser(CapError("You've hit the limit")), key="k")
        r = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status == 401
        r = await c.post("/v1/chat/completions", headers={"Authorization": "Bearer k"},
                         json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status == 429 and "limit" in (await r.json())["error"]["message"]
        await c.close()
    run(go())
