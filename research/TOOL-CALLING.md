# Tool calling and prompt size: research (2026-11-25)

chatgpt.com has no client tool calling that gptapi can reach. Its own tools, such as web
search, run on OpenAI's servers, not on the user's computer. So gptapi has to **emulate** tool
calling:
1. Describe the client's tools in the prompt.
2. Have the model reply with tool-call blocks in a fixed format.
3. Turn those blocks into OpenAI `tool_calls`.
4. Send the tool results back as the next message.

This file records what was tested live and what to build. Scripts and opencode configs are in
`tool-calling/`. The raw captures are in `results/tool-calling/`, which is not in git because
they contain local paths and the user's CLAUDE.md.

## 1. What opencode 2.4.0 sends

The first request of a session, with the default agent, captured by `tool-calling/capture.py`:

| Part | Characters |
|---|---|
| System prompt | 14,095 |
| of which: Code Mode instructions (for the `execute` tool) | 8,774 |
| of which: the user's `~/.claude/CLAUDE.md`, which opencode reads for Claude Code compatibility | ~1,850 |
| of which: skills list | 921 |
| of which: core instructions plus `<env>` | ~2,500 |
| 14 tool definitions as JSON | 18,389 |
| **Total** | **~33,000** |

The tools are edit, read, write, glob, grep, shell, webfetch, websearch, question, skill, subagent,
execute, generate_image and generate_video. The media and subagent tools alone take about 7,000
characters.

## 2. Live tests on chatgpt.com (through gptapi, 8 logged-out messages)

| # | Test | Result |
|---|---|---|
| T1 | Full 32k prompt, tool protocol **first** (system message) | Accepted, answered in 13 s. The model refused the role and said a listing tool "isn't available". |
| T2 | Same content, protocol **last**, with an example and a one-line-per-tool list (17.6k) | Correct `shell` call with `ls -la`. It copied an extra `description` argument from the example, which isn't in the schema. |
| T3 | Real `ls` output sent back as `[tool result shell]` | Correct final answer from the result |
| T4 | A question that needs a file read, **no reminder** | Failed: it guessed the answer instead of calling `read` |
| T5 | Same kind of question, **with a one-line reminder** | Correct `grep` call with arguments that match the schema |
| T6 | Edit with quotes, backslashes, `$HOME` and backticks | Valid JSON; every character survived (inside a fenced block, gptapi's unescaping leaves it alone) |

The format that worked:

````
```tool_call
{"name": "shell", "arguments": {"command": "ls -la"}}
```
````

Findings:
- 32k characters fit in one message. The limit wasn't reached.
- Instructions buried in a long prompt get ignored. Put the protocol at the end, right after
  the user request.
- The model drifts after a few turns. Add a short reminder to every message.
- Arguments must be checked against each tool's schema, and unknown keys dropped.
- **Each agent step is one chatgpt.com message.** A small task takes about 5–20 steps, and the
  logged-out daily cap is small. This is the real limit, not the format.
- Bug found: a "We use cookies" banner appeared during one send and the typed text was lost
  ("not accepted"). gptapi should click **Reject non-essential** when the banner appears.

## 3. Shrinking opencode's prompt

All of these were captured with the dummy server, so they cost no chatgpt.com messages.

| Config | System | Tools | Total |
|---|---|---|---|
| Default | 14,095 | 14 tools, 18,389 | **~33,000** |
| A: deny 8 tools, media generation off | 4,435 | 21 tools, 18,384 (denying `execute` exposes 15 `automation_*`/`opencode_*` tools) | 22,800 |
| B: A plus a custom agent with its own short system prompt | 2,800 | 21, 18,384 | 21,200 |
| C: B plus `automation_*`/`opencode_*` denied, and opencode's server started with `OPENCODE_DISABLE_CLAUDE_CODE*=1` | **777** | 8 tools, 7,762 | **8,500** |
| D: same denies **inside the agent** only (other agents untouched), CLAUDE.md still on | 2,584 | 8, 7,762 | 10,300 |

Notes:
- **Denies:** `skill` and `opencode_worktree_remove` stay in the list even when denied.
- **CLAUDE.md:** the `OPENCODE_DISABLE_CLAUDE_CODE*` variables only take effect on opencode's
  background server (`opencode service`), not on the `opencode run` client. They switch
  CLAUDE.md off for every model, not just gptapi.
- **Custom agent:** an agent's `system` replaces opencode's Harness, Communication and Working
  in codebases sections, about 1,600 characters of guidance.
- **Compact tool list:** gptapi can rewrite the remaining JSON tool schemas (~7.7k) as one-line
  signatures (~1.5k), the format T2 used. That leaves a first message of about 3–4k characters.

## 4. Plan (built in 0.0.2; see README "Tool calling")

1. **opencode side:** a `gptapi` agent with `model: gptapi/chatgpt-web`, a short system prompt
   and the denies from config D (`tool-calling/opencode-D.json`).
2. **gptapi side, when `tools` are present:**
   - put the protocol and the compact tool list last in the first message, and a reminder in
     every message;
   - show earlier tool calls and results as text, and include them in history hashing;
   - parse the `tool_call` blocks and check them against each tool's schema;
   - return `tool_calls`, holding back streamed output once a tool-call block starts;
   - cut very large tool results to a safe length.
3. **Cookie-banner fix:** click Reject non-essential.
4. **Pilot:** a few small opencode tasks, counting messages per task.

## 5. Decisions (2026-11-25)

Based on `OPENCODE-FLOW.md` (what opencode sends) and `LIMITS.md` (window ~24–28k tokens,
~100k characters of real content; the oldest messages drop silently):

| Topic | Decision |
|---|---|
| Where to slim | Entirely in gptapi. No custom opencode agent; vLLM isn't used in this project. `opencode.json` keeps only "titles off". |
| System prompt | **Replace** opencode's prompt (15.5k chars) with gptapi's own short coding-agent prompt plus opencode's `<env>` block (~1k chars). |
| Tools | Allowed-tool list (default edit, glob, grep, read, shell, write), one line per tool (~1.4k), protocol last, a one-line reminder each turn, schema check on every call. |
| Long conversations | **gptapi rollover.** Track how many characters each chatgpt.com conversation holds. Past ~70k, start a fresh conversation in the same request: slim prompt + tool list + the original task (first user message) + the most recent turns that fit. No extra message. |
| Large tool results | **Cap and let the model page.** Send the first ~6k and last ~2k characters with a note: "N chars omitted; use read with offset/limit or grep". Don't push chunks up front: measured, it fits nothing more and costs a message per chunk. |
| Usage numbers | Still to decide. Reporting the real chatgpt.com conversation size would make opencode's context display truthful. |
| Small fixes | Cookie banner: click "Reject non-essential". chatgpt.com's HTTP 400 for an oversized message should be `message_too_long`, not `rate_limit_exceeded`. |

## 6. Built and piloted (0.0.2, 2026-11-25)

All of §5 is built except "usage numbers" (still the chars typed this turn / 4). 37 unit tests
pass. Pilot with opencode 2.4.0; raw records in `results/pilot/`:

| Task | chatgpt.com messages | Prompts typed | Result |
|---|---|---|---|
| "What does calc.py do?" | 2 | 2,529 → 342 | read, then a correct answer |
| "Add sub(a, b), a test, run pytest" | 3 | 3,870 → 537 → 343 | one `patch` to two files, `shell` pytest, summary; the edit was correct (3 tests pass) |

Found in the pilot: for a model named `chatgpt-web` (contains "gpt"), opencode offers `patch`
(an apply-patch diff format) instead of `edit`/`write`. `patch` is now allowed, and its full
description is sent.

## 7. Long task with a live rollover (2026-11-25)

Task, in a 4-file to-do app: "add a priority field (Task, to_dict/from_dict, Store.add, a CLI
option, '!' in fmt, Store.by_priority), add tests, run them". To force a rollover early, the
run used `GPTAPI_ROLLOVER_CHARS=12000` (set in `docker/.env`). The recordings are in
`results/rollover/`, which is not in git.

| Run | Model id / edit tool | What happened |
|---|---|---|
| 1 | `chatgpt-web` / `patch` | 4 messages: reads, patch, **rollover** (4.4k-char seed), re-reads + tests, summary. 6 tests pass. The seed held only the task and `[tool result patch]`: the patch call itself didn't fit the 6k half-budget. **Fix:** always keep the latest turn, shortened if needed. |
| 2 | `chatgpt-web` / `patch` | The first answer was a refusal: "I can't access the workspace through the coding tools". **Fix:** stronger wording ("the tool connection is live") and one automatic nudge. |
| 3 | `chatgpt-web` / `patch` | Another first-turn refusal ("tools aren't currently available"), which the first refusal pattern missed. **Fix:** a wider pattern. |
| 4 | `chatgpt-web` / `patch` | Reads worked. Then: "I couldn't apply the patch through the available execution interface". The model was calling its own built-in apply-patch tool. **Fix:** nudge on any turn, not just the first, and hold short text back from the stream. |
| 5 | `chatgpt-web` / `patch` | Refusal on turn 1 was nudged and recovered. Turn 2 had the same "alternate execution interface" failure. |
| 6 | **`chat-web` / `edit`+`write`** | Refusal on turn 1, nudged; reads; edits to 4 files; **rollover** (the seed kept the latest edits); a test run showed a missing `)`; the model fixed it; 7 tests pass. **6 chatgpt.com messages in total.** |

Findings:
- **Edit tool:** opencode picks the edit tool from the model id. With "gpt" in the id it offers
  `patch` (OpenAI's apply-patch format), and chatgpt.com's model then tries its own built-in
  apply-patch tool. Two runs out of two failed that way. `edit` (JSON oldString/newString)
  works, as T6 had already shown. The model id is now `chat-web`.
- **Refusals:** in about half of the fresh conversations today, the first answer claimed the
  tools weren't connected. Stronger wording alone didn't stop it. One nudge in the same
  conversation recovered every time it was tried (3 of 3), at the cost of one message.
- **Rollover:** it works live. The task carried on in the new conversation without
  re-planning, and opencode didn't notice anything.

Not yet tested live: the cookie-banner retype (no banner appeared in these runs).
