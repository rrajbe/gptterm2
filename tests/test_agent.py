"""Agent mode: emulated tool calling, lean prompts, capping, rollover (fake browser, no network)."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from gptapi.agent import REMINDER, SYSTEM, Agent
from gptapi.config import Config
from gptapi.server import APP_KEY, build_app

TOOLS = [
    {"type": "function", "function": {"name": "read", "description": "Read a file.\nMore text.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                    "required": ["path"]}}},
    {"type": "function", "function": {"name": "shell", "description": "Run a command.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "generate_video", "description": "Make a video.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}}},
]
SYS = "You are opencode. Lots of text.\n# Code Mode\n" + "x" * 5000 + "\n<env>\n  Working directory: /w\n</env>"
CALL = '```tool_call\n{"name": "read", "arguments": {"path": "a.py", "description": "extra"}}\n```'


def agent(**kw):
    return Agent(TOOLS, **kw)


# ---------------------------------------------------------------- pure
def test_allowlist_and_catalog():
    a = agent()
    assert set(a.tools) == {"read", "shell"} and a.dropped == ["generate_video"]
    cat = a.catalog()
    assert "- read(path: string, offset?: integer): Read a file." in cat and "More text" not in cat
    assert set(Agent(TOOLS, allow=("*",)).tools) == {"read", "shell", "generate_video"}
    # a client whose tools are all outside the allowlist keeps them
    assert set(Agent(TOOLS, allow=("nope",)).tools) == {"read", "shell", "generate_video"}
    assert Agent([{"x": 1}]).tools == {}


def test_parse_valid_invalid_and_extra_args():
    a = agent()
    text, calls, problems = a.parse("Let me look.\n\n" + CALL + "\n\n```tool_call\n{\"name\": \"rm\", \"arguments\": {}}\n```")
    assert calls == [("read", {"path": "a.py"})]          # unknown 'description' dropped
    assert "Let me look." in text and "rm" in text           # invalid block stays visible
    assert any("unknown tool" in p for p in problems)
    _, calls, problems = a.parse('```tool_call\n{"name": "read", "arguments": {}}\n```')
    assert calls == [] and "missing required" in problems[0]
    _, calls, _ = a.parse('<tool_call>{"name": "shell", "arguments": {"command": "ls"}}</tool_call>')
    assert calls == [("shell", {"command": "ls"})]
    text, calls, _ = a.parse("Just an answer.")
    assert text == "Just an answer." and calls == []


def test_transform_lean_system_merge_and_cap():
    a = agent(result_chars=100)
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.py"}'}},
                {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": '{"command":"ls"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "y" * 500},
            {"role": "tool", "tool_call_id": "c2", "content": "a.py b.py"}]
    out = a.transform(msgs)
    assert out[0]["role"] == "system" and out[0]["content"].startswith(SYSTEM)
    assert "<env>" in out[0]["content"] and "Code Mode" not in out[0]["content"]
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert out[2]["content"].count("```tool_call") == 2
    merged = out[3]["content"]
    assert merged.startswith("[tool result read]") and "[tool result shell]\na.py b.py" in merged
    assert "characters omitted" in merged and len(merged) < 400


def test_render_roundtrip_is_canonical():
    a = agent()
    text, calls, _ = a.parse("Reading.\n\n" + CALL)
    ours = a.render_assistant(text, calls)
    echoed = a.transform([{"role": "assistant", "content": "Reading.", "tool_calls": Agent.to_openai(calls)}])[1]
    assert echoed["content"] == ours


def test_first_prompt_layout_and_budget():
    a = agent()
    prefix = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "THE TASK"}]
    for i in range(30):
        prefix += [{"role": "assistant", "content": f"answer {i} " + "z" * 900},
                   {"role": "user", "content": f"result {i} " + "w" * 900}]
    p = a.first_prompt(prefix, {"role": "user", "content": "NOW"})
    assert p.index("[Instructions]") < p.index("[Conversation so far]") < p.index("[Current message]") < p.index("[Tool protocol]")
    small = a.first_prompt(prefix, {"role": "user", "content": "NOW"}, budget=8000)
    assert len(small) < 9000 and "THE TASK" in small and "result 29" in small and "result 0 " not in small
    assert "older messages were left out" in small


# ---------------------------------------------------------------- server
CID1 = "6a1f0c2e-1111-4222-8333-944455556661"
CID2 = "6a1f0c2e-1111-4222-8333-944455556662"


class ScriptedBrowser:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []
        self.human = {"required": False}
        self.connected = True

    async def ensure(self):
        pass

    async def health(self):
        return {"connected": True}, []

    def shutdown(self):
        pass

    async def turn(self, prompt, conv_id, inject, on_event):
        self.calls.append((prompt, conv_id, inject))
        cid = conv_id or (CID1 if len([c for c in self.calls if c[1] is None]) == 1 else CID2)
        on_event("conv", cid)
        ans = self.answers.pop(0)
        on_event("text", ans)
        await asyncio.sleep(0)
        return {"conversation_id": cid, "answer": ans, "message_id": "m",
                "conv_state": {"backendConversationId": cid, "parentMessageId": f"p{len(self.calls)}"},
                "complete": True, "incomplete_reason": None, "retracted": False}


async def _client(tmp_path, fake, **cfg):
    app = build_app(Config(data=str(tmp_path), **cfg))
    app[APP_KEY].browser = fake
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _sse(raw):
    return [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]


def test_tool_loop_nonstream_matches_and_reminds(tmp_path):
    async def go():
        fake = ScriptedBrowser(["Looking.\n\n" + CALL, "It defines f()."])
        c = await _client(tmp_path, fake)
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": "what is in a.py?"}]
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": msgs, "tools": TOOLS})
        j = await r.json()
        ch = j["choices"][0]
        assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] == "Looking."
        tc = ch["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "read" and json.loads(tc["function"]["arguments"]) == {"path": "a.py"}
        first = fake.calls[0][0]
        assert first.startswith("[Instructions]\n" + SYSTEM) and first.rstrip().endswith(
            "When no tool is needed, answer normally without any tool_call block.")
        assert "Code Mode" not in first and "generate_video" not in first
        # opencode sends the tool result back with the full history
        msgs += [{"role": "assistant", "content": "Looking.", "tool_calls": ch["message"]["tool_calls"]},
                 {"role": "tool", "tool_call_id": tc["id"], "content": "def f(): pass"}]
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": msgs, "tools": TOOLS})
        j = await r.json()
        assert r.headers["X-Gptapi-Mapping"] == "matched"
        assert j["choices"][0]["message"]["content"] == "It defines f()." and j["choices"][0]["finish_reason"] == "stop"
        prompt, conv, _ = fake.calls[1]
        assert conv == CID1 and prompt == "[tool result read]\ndef f(): pass\n\n" + REMINDER
        await c.close()
    asyncio.run(go())


def test_tool_call_streaming(tmp_path):
    async def go():
        fake = ScriptedBrowser(["Checking.\n\n" + CALL])
        c = await _client(tmp_path, fake)
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": "go"}]
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": msgs, "tools": TOOLS, "stream": True})
        chunks = _sse(await r.text())
        text = "".join(ch["choices"][0]["delta"].get("content") or "" for ch in chunks if ch.get("choices"))
        assert text == "Checking." and "tool_call" not in text
        tcs = [d for ch in chunks if ch.get("choices") for d in ch["choices"][0]["delta"].get("tool_calls") or []]
        assert len(tcs) == 1 and tcs[0]["index"] == 0 and tcs[0]["function"]["name"] == "read"
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
        await c.close()
    asyncio.run(go())


def test_rollover_when_conversation_is_full(tmp_path):
    async def go():
        fake = ScriptedBrowser(["ok 1", "ok 2", "ok 3", "ok 4"])
        c = await _client(tmp_path, fake, rollover_chars=4000)
        post = lambda m: c.post("/v1/chat/completions", json={"model": "m", "messages": m, "tools": TOOLS})
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": "THE TASK"}]
        await post(msgs)
        msgs += [{"role": "assistant", "content": "ok 1"}, {"role": "user", "content": "second"}]
        r = await post(msgs)
        assert r.headers["X-Gptapi-Mapping"] == "matched"
        msgs += [{"role": "assistant", "content": "ok 2"}, {"role": "user", "content": "big " + "q" * 3000}]
        r = await post(msgs)
        assert r.headers["X-Gptapi-Mapping"] == "rollover"
        prompt, conv, inject = fake.calls[2]
        assert conv is None and inject is None
        assert "THE TASK" in prompt and "[Current message]\nbig" in prompt and "[Tool protocol]" in prompt
        assert r.headers["X-Conversation-Id"] == CID2
        app = c.server.app[APP_KEY]
        assert app.store.conv(CID1)["rolled_to"] == CID2 and app.store.follow(CID1) == CID2
        # the next turn continues in the new conversation (one turn old: never rolled again at once)
        msgs += [{"role": "assistant", "content": "ok 3"}, {"role": "user", "content": "small"}]
        r = await post(msgs)
        assert r.headers["X-Gptapi-Mapping"] == "matched" and fake.calls[3][1] == CID2
        await c.close()
    asyncio.run(go())


def test_plain_requests_unchanged(tmp_path):
    async def go():
        fake = ScriptedBrowser(["hello"])
        c = await _client(tmp_path, fake)
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": [
            {"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]})
        j = await r.json()
        assert j["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
        assert fake.calls[0][0].startswith("[System instructions]\nS")
        await c.close()
    asyncio.run(go())


def test_patch_tool_gets_full_description():
    patch = {"type": "function", "function": {"name": "patch", "description": "Edit files.\n\n*** Begin Patch\n+x\n*** End Patch",
             "parameters": {"type": "object", "properties": {"patchText": {"type": "string"}}, "required": ["patchText"]}}}
    a = Agent(TOOLS + [patch])
    assert "patch" in a.tools and "*** Begin Patch" in a.catalog()
