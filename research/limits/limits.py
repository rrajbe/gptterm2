"""Measure logged-out chatgpt.com limits through gptapi (each call = one chatgpt.com message).

  python3 limits.py size <chars> [<chars> ...]     # E1: biggest single message, and is it intact?
  python3 limits.py long <turns> <chars-per-turn>  # E2: what the model remembers as a conversation grows

Results are appended as JSON lines to results/limits.jsonl (relative to the cwd).
"""
import json, os, random, sys, time, urllib.request

API = os.environ.get("GPTAPI_URL", "http://127.0.0.1:8000/v1/chat/completions")
WORDS = ("river stone paper window garden yellow quiet machine travel orange winter signal "
         "market pencil bridge silver cotton planet engine forest candle rocket harbor velvet "
         "meadow copper lantern puzzle saddle timber violet walnut anchor basket cactus dragon "
         "falcon glacier hammer island jungle kettle lemon magnet nectar oyster pepper quartz").split()
OUT = "results/limits.jsonl"


def filler(n, seed):
    rnd = random.Random(seed)
    out, size = [], 0
    while size < n:
        s = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(8, 16))).capitalize() + "."
        out.append(s)
        size += len(s) + 1
    return " ".join(out)[:n]


def send(conv, text):
    body = json.dumps({"model": "chatgpt-web", "stream": False,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json",
                                                          "X-Conversation-Id": conv})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=400) as r:
            j = json.load(r)
        ans = j["choices"][0]["message"]["content"]
        err = None
    except urllib.error.HTTPError as e:
        ans, err = None, json.loads(e.read() or b"{}").get("error", {"code": e.code})
    return ans, err, round(time.time() - t, 1)


def log(rec):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec, ensure_ascii=False)[:600], flush=True)


def size_test(sizes):
    for n in sizes:
        tag = f"S{n}-{random.randint(1000, 9999)}"
        head, tail = f"START-MARKER {tag}-A.", f"END-MARKER {tag}-Z."
        q = ("\n\nThe text above is filler for a size test. Reply with exactly two lines: the code after "
             "START-MARKER and the code after END-MARKER, nothing else.")
        text = head + " " + filler(n - len(head) - len(tail) - len(q) - 2, n) + " " + tail + q
        ans, err, secs = send(f"limits-size-{tag}", text)
        ok = bool(ans) and f"{tag}-A" in ans and f"{tag}-Z" in ans
        log({"test": "size", "chars": len(text), "est_tokens": len(text) // 4, "accepted": err is None,
             "start_seen": bool(ans) and f"{tag}-A" in ans, "end_seen": bool(ans) and f"{tag}-Z" in ans,
             "intact": ok, "seconds": secs, "answer": ans, "error": err})
        if err:
            break


def long_test(turns, per_turn):
    run = random.randint(1000, 9999)
    conv = f"limits-long-{run}"
    secret = f"PURPLE-{run}"
    first = (f"Remember these two things for the whole conversation.\n"
             f"1. The secret code is {secret}.\n"
             f"2. Rule: end every reply with the exact text ~END~\n"
             f"I will now send several long filler messages, each tagged T<n>. For each one, reply with "
             f"one line: the tag of that message, the secret code, and the tag of the first filler "
             f"message (T1) if you can still see it, otherwise 'T1 gone'. Then the rule. "
             f"Reply now with: ready, the secret code, and the rule.")
    total = len(first)
    ans, err, secs = send(conv, first)
    log({"test": "long", "run": run, "turn": 0, "conv_chars": total, "answer": ans, "error": err, "seconds": secs})
    for t in range(1, turns + 1):
        tag = f"T{t}-{run}"
        q = (f"\n\n[This filler message is tagged {tag}.] Reply with one line: this message's tag, the "
             f"secret code from my first message, and the tag of filler message T1 (or 'T1 gone'), then "
             f"follow the rule from my first message.")
        text = f"[Filler {tag} begins] " + filler(per_turn - len(q) - 30, run * 100 + t) + q
        ans, err, secs = send(conv, text)
        total += len(text) + len(ans or "")
        a = ans or ""
        log({"test": "long", "run": run, "turn": t, "conv_chars": total, "est_tokens": total // 4,
             "tag_ok": tag in a, "secret_ok": secret in a, "t1_ok": f"T1-{run}" in a,
             "rule_ok": "~END~" in a, "seconds": secs, "answer": a[:300], "error": err})
        if err:
            break


if __name__ == "__main__":
    if sys.argv[1] == "size":
        size_test([int(x) for x in sys.argv[2:]])
    elif sys.argv[1] == "long":
        long_test(int(sys.argv[2]), int(sys.argv[3]))
