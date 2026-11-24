import json

import pytest

from gptapi.history import flatten, has_dialogue, history_hash, message_prompt, split_request, content_text
from gptapi.mapping import after_turn_hashes, resolve, model_name, BadRequest
from gptapi.store import Store, TurnRecord
from gptapi.stream import StableEmitter, StreamParser, normalize_answer, split_blocks

CID = "6a1f0c2e-1111-4222-8333-944455556666"


def nd(*objs):
    return b"".join(json.dumps(o).encode() + b"\n" for o in objs)


def content(text, status="streaming", rev=1):
    return {"type": "content", "messageId": "m1", "revision": rev, "mode": "replace", "baseRevision": rev - 1,
            "markdown": "", "dil": {"fallbackMarkdown": text}, "status": status}


STREAM = [
    {"type": "control", "detail": {"kind": "started"}},
    {"type": "control", "detail": {"kind": "conversation-id", "conversationId": CID}},
    {"type": "shared", "html": "<div>ignored</div>"},
    {"type": "control", "detail": {"kind": "assistant-content-started"}},
    content("Hello", rev=1),
    content("Hello there.\n\nSecond", rev=2),
    content("Hello there.\n\nSecond para.\n\n```py\nx = 1\n\ny = 2", rev=3),
    content("Hello there.\n\nSecond para.\n\n```py\nx = 1\n\ny = 2\n```\n\nDone.", status="complete", rev=4),
    {"type": "state", "conversationState": {"backendConversationId": CID, "parentMessageId": "m1", "userMessageCount": 1}},
    {"type": "control", "detail": {"kind": "last-token-received"}},
    {"type": "end"},
]


# ---------------------------------------------------------------- stream parsing

def test_parser_full_stream():
    p = StreamParser()
    p.feed(nd(*STREAM))
    assert p.conv_id == CID and p.started and p.done and p.complete
    assert p.conv_state["parentMessageId"] == "m1"
    assert p.answer().endswith("Done.")
    assert "```py\nx = 1\n\ny = 2\n```" in p.answer()


def test_parser_split_chunks_and_flush():
    raw = nd(*STREAM)
    p = StreamParser()
    for i in range(0, len(raw), 7):        # arbitrary chunk boundaries
        p.feed(raw[i:i + 7])
    p.flush()
    assert p.done and p.answer().endswith("Done.")


def test_parser_keeps_fullest_on_retraction():
    p = StreamParser()
    p.feed(nd(content("A long answer that will be pulled back.", rev=1), content("Sorry.", status="complete", rev=2)))
    assert p.text == "Sorry."
    assert p.answer() == "A long answer that will be pulled back."


def test_parser_error_and_garbage():
    p = StreamParser()
    p.feed(b"not json\n" + nd({"type": "control", "detail": {"kind": "rate-limit", "x": 1}}))
    assert p.error and "rate-limit" in p.error


def test_parser_append_mode_without_fallback():
    p = StreamParser()
    p.feed(nd({"type": "content", "mode": "append", "markdown": "ab"}, {"type": "content", "mode": "append", "markdown": "cd"}))
    assert p.text == "abcd"


# ---------------------------------------------------------------- blocks / stable deltas

def test_split_blocks_respects_fences():
    md = "a\n\n```\nx\n\ny\n```\n\nb"
    assert split_blocks(md) == ["a", "```\nx\n\ny\n```", "b"]


def test_emitter_only_emits_finished_blocks():
    e = StableEmitter()
    out = [e.feed(c["dil"]["fallbackMarkdown"]) for c in STREAM if c.get("type") == "content"]
    assert out[0] == ""                         # "Hello" still growing
    assert out[1] == "Hello there."             # first block finished
    assert out[2] == "\n\nSecond para."         # fence still open -> not emitted
    assert out[3] == "\n\n```py\nx = 1\n\ny = 2\n```"
    tail = e.finish(STREAM[7]["dil"]["fallbackMarkdown"])
    assert tail == "\n\nDone."
    assert e.sent == normalize_answer(STREAM[7]["dil"]["fallbackMarkdown"])


def test_emitter_never_retracts():
    e = StableEmitter()
    e.feed("one\n\ntwo\n\nthr")
    sent = e.sent
    e.feed("ONE rewritten\n\ntwo\n\nthree")   # earlier block rewritten: nothing re-emitted
    assert e.sent.startswith(sent)
    e.finish("ONE rewritten\n\ntwo\n\nthree")
    assert e.sent == "one\n\ntwo\n\nthree"


def test_emitter_shrinking_final():
    e = StableEmitter()
    e.feed("a\n\nb\n\nc\n\nd")
    assert e.finish("a") == ""


# ---------------------------------------------------------------- history

MSGS = [
    {"role": "system", "content": "Be terse."},
    {"role": "user", "content": "What is 2+2?"},
    {"role": "assistant", "content": "4"},
    {"role": "user", "content": [{"type": "text", "text": "And times 3?"}]},
]


def test_content_text_parts():
    assert content_text([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}]) == "a\n[image omitted]"
    assert content_text(None) == ""


def test_split_and_hash_whitespace_insensitive():
    prefix, last = split_request(MSGS)
    assert last["content"][0]["text"] == "And times 3?"
    assert has_dialogue(prefix)
    alt = [dict(m) for m in prefix]
    alt[2] = {"role": "assistant", "content": "  4\n"}
    assert history_hash(prefix) == history_hash(alt)
    assert history_hash(prefix) != history_hash(prefix[1:])
    assert history_hash(prefix, ignore_system=True) == history_hash(prefix[1:])


def test_assistant_tool_call_only_dropped():
    msgs = [{"role": "user", "content": "x"}, {"role": "assistant", "content": None, "tool_calls": [{}]}]
    assert history_hash(msgs) == history_hash(msgs[:1])


def test_flatten():
    prefix, last = split_request(MSGS)
    f = flatten(prefix, last)
    assert f.startswith("[System instructions]\nBe terse.")
    assert "User: What is 2+2?\n\nAssistant: 4" in f
    assert "[Current message]\nAnd times 3?" in f
    assert flatten([], {"role": "user", "content": "hi"}) == "hi"
    assert flatten([{"role": "system", "content": "S"}], {"role": "user", "content": "hi"}) == \
        "[System instructions]\nS\n\n[Current message]\nhi"
    assert message_prompt({"role": "tool", "content": "42"}) == "[Tool result]\n42"


def test_split_request_empty():
    assert split_request([]) == ([], None)
    assert split_request([{"role": "user", "content": "x"}, {"role": "assistant", "content": ""}])[1]["content"] == "x"


# ---------------------------------------------------------------- mapping

@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path))


def test_resolve_new_then_match(store):
    first = MSGS[:2]
    plan = resolve(store, first, "gpt-4o")
    assert plan.mode == "new" and plan.conv_id is None
    assert plan.prompt.startswith("[System instructions]")
    state = {"backendConversationId": CID, "parentMessageId": "m1", "userMessageCount": 1}
    store.add_point(CID, after_turn_hashes(plan.prefix, plan.last, ["4"]), state)

    plan2 = resolve(store, MSGS, "gpt-4o")
    assert plan2.mode == "matched" and plan2.conv_id == CID and plan2.inject is None
    assert plan2.prompt == "And times 3?"

    # system prompt changed (e.g. date in it): still matched, ignoring system
    changed = [{"role": "system", "content": "Be terse. Today is Tuesday."}] + MSGS[1:]
    plan3 = resolve(store, changed)
    assert plan3.mode == "matched-nosystem" and plan3.conv_id == CID


def test_resolve_branch_from_older_point(store):
    s1 = {"backendConversationId": CID, "parentMessageId": "m1", "userMessageCount": 1}
    s2 = {"backendConversationId": CID, "parentMessageId": "m2", "userMessageCount": 2}
    store.add_point(CID, after_turn_hashes(MSGS[:1], MSGS[1], ["4"]), s1)
    store.add_point(CID, after_turn_hashes(MSGS[:3], MSGS[3], ["12"]), s2)
    # client regenerates the 2nd answer: history = turn 1 only, new last message
    plan = resolve(store, MSGS[:3] + [{"role": "user", "content": "And times 5?"}])
    assert plan.mode == "branch" and plan.inject == s1
    plan = resolve(store, MSGS + [{"role": "assistant", "content": "12"}, {"role": "user", "content": "more"}])
    assert plan.mode == "matched" and plan.inject is None


def test_resolve_no_dialogue_never_matches(store):
    store.add_point(CID, [history_hash([{"role": "user", "content": "hi"}])], {"x": 1})
    plan = resolve(store, [{"role": "user", "content": "hi"}, {"role": "user", "content": "again"}])
    assert plan.mode == "new"


def test_resolve_named_and_header(store):
    assert model_name("chatgpt-web/work") == "work" and model_name("chatgpt-web") is None
    plan = resolve(store, MSGS[:2], "chatgpt-web/work")
    assert plan.mode == "new" and plan.name == "work"
    store.add_point(CID, [], {"backendConversationId": CID})
    store.bind_name("work", CID)
    plan = resolve(store, [{"role": "user", "content": "next"}], "chatgpt-web/work")
    assert plan.mode == "named" and plan.conv_id == CID and plan.prompt == "next"
    plan = resolve(store, [{"role": "user", "content": "next"}], "x", header_conv=CID)
    assert plan.mode == "pinned" and plan.conv_id == CID
    plan = resolve(store, [{"role": "user", "content": "next"}], "x", header_conv="work")
    assert plan.mode == "named"
    with pytest.raises(BadRequest):
        resolve(store, [])


def test_turnrecord_transcript(store, tmp_path):
    p = StreamParser()
    rec = TurnRecord(store, "hello", None)
    p.feed(nd(*STREAM[:6]))
    rec.set_conv(p.conv_id)
    rec.snapshot(p.message_id, p.best, force=True)
    import os
    assert os.listdir(store.inflight)
    p.feed(nd(*STREAM[6:]))
    rec.finish(p, 1.0)
    lines = [json.loads(l) for l in open(os.path.join(store.sessions, CID + ".jsonl"))]
    assert [l["role"] for l in lines] == ["user", "assistant"]
    assert "incomplete" not in lines[1] and lines[1]["text"].endswith("Done.")
    assert not os.listdir(store.inflight)


def test_recover_inflight(store):
    import os
    rec = TurnRecord(store, "lost prompt", CID)
    rec.snapshot("m9", "partial ans", force=True)
    Store(store.data).recover_inflight()
    lines = [json.loads(l) for l in open(os.path.join(store.sessions, CID + ".jsonl"))]
    assert lines[-1]["incomplete"] and lines[-1]["text"] == "partial ans"
    assert lines[0]["text"] == "lost prompt"


def test_unescape_md():
    from gptapi.stream import unescape_md
    assert unescape_md("The capital is Paris\\! (yes\\.)") == "The capital is Paris! (yes.)"
    assert unescape_md("keep \\*star\\* and \\_u\\_") == "keep \\*star\\* and \\_u\\_"
    assert unescape_md("1\\. not a list\\.") == "1\\. not a list."
    assert unescape_md("\\- not a bullet\\!") == "\\- not a bullet!"
    assert unescape_md("code `a\\!b` out\\!") == "code `a\\!b` out!"
    assert unescape_md("```\nx\\!\n```\ny\\!") == "```\nx\\!\n```\ny!"
    assert normalize_answer("Paris\\!") == "Paris!"
