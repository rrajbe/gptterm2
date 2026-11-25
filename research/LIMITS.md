# chatgpt.com logged-out limits, measured (2026-11-25)

Measured through gptapi with `limits/limits.py`, using 17 logged-out messages in total
(including the 2026-11-25 tool tests). Raw records are in `results/limits.jsonl`, not in git.
Token counts use tiktoken `o200k_base`, the GPT-4o/5-era tokenizer, which is close enough for
budgeting.

## E1: largest single message

The message has a marker at its start and at its end, with filler in between. The model is
asked to repeat both markers, which proves the whole message reached it.

| Characters | Tokens (o200k) | Result |
|---|---|---|
| 60,000 | ~9,400 | accepted, both markers seen (4.2 s) |
| 120,000 | ~18,800 | accepted, both markers seen (4.6 s) |
| 180,000 | ~28,300 | **rejected**, chatgpt.com HTTP 400 |
| 240,000 | ~37,700 | rejected, HTTP 400 |

The per-message limit lies between ~19k and ~28k tokens. When a message is accepted, nothing
is cut silently; an oversized one is refused outright.

Bug: gptapi reports that HTTP 400 as `rate_limit_exceeded`. It should be a distinct
`message_too_long` error.

## E2: memory over a long conversation

Turn 0 plants a secret code and a rule ("end every reply with ~END~"). Then come 12 filler
messages of 30,000 characters each. Every turn asks for that message's tag, the secret code,
and whether filler T1 is still visible.

| Turn | Conversation size | Tokens (o200k) | Tag | Secret | T1 | Rule |
|---|---|---|---|---|---|---|
| 1 | 30k chars | ~4.8k | yes | yes | yes | yes |
| 2–5 | 60k–150k | ~9.5k–23.5k | yes | yes | yes | yes |
| **6** | **180k** | **~28.3k** | yes | **no** | **no** | **no** |
| 7–12 | 210k–361k | ~33k–57k | yes | no | no | no |

At turn 6 the model said: "I can't see the first message's secret code or rule, T1 gone." It
never recovered.

The context window is about 24–28k tokens. That matches the ~27K one 2026 guide gives for
signed-in Free users, and it's about the same size as the per-message limit. When the window
fills, the **oldest messages drop out silently and together**: the first message and T1 went
in the same turn. The model only notices because it was asked.

## What that means in real content

| Content | Chars per token | 27k tokens ≈ |
|---|---|---|
| Filler (common English words) | 6.4 | 170k chars |
| Python code (`gptapi/browser.py`) | 4.3 | 115k chars |
| Markdown (`README.md`) | 3.8 | 100k chars |

**Planning number: one chatgpt.com conversation holds about 100k characters of real code and
text, counting everything typed and everything received.**

- opencode's full system prompt (15.5k chars, ~4k tokens) is **15%** of the window. The slim
  replacement (~1k chars) is about 1%.
- One full read of `browser.py` (25k chars) is about **20%**.

## Consequences for gptapi

1. **The first message is the first thing to be dropped.** Whatever gptapi puts there (the
   instructions, the tool protocol, the user's task) disappears without warning once about
   100k characters have passed. A per-turn reminder helps only for the protocol, not for the
   task.
2. **Chunking large tool results doesn't make more fit.** The per-message limit is about the
   window size, so chunks still pile into the same ~27k window and push out older context
   sooner. Each chunk also costs a message, because the model replies to each one. Chunking
   only helps when the model **asks** for more (paging), not when it's pushed up front.
3. **gptapi's `prompt_tokens` (chars typed this turn / 4) hides all of this from opencode.**
