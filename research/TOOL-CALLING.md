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

## 4. Plan (not built yet)

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
