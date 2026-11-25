# How opencode talks to chatgpt.com through gptapi (reference, 2026-11-25)

Recorded with opencode 2.4.0, gptapi 0.0.1 and the repo's `opencode.json` (titles off).
`tool-calling/record-proxy.py` sat between opencode and gptapi (opencode → `127.0.0.1:18112`
→ gptapi `:8000`) and saved every request together with gptapi's `X-Gptapi-Mapping` and
`X-Conversation-Id` headers. What reached chatgpt.com was read from gptapi's transcript
(`/data/gptapi/sessions/<id>.jsonl`). Raw records are in `results/opencode-flow/`, which is not
in git.

Session: "Pick a random animal and tell me only its name." → "Axolotl", then `opencode run -c`
"What was the animal you picked? Answer with one word." → "Axolotl".

## 1. opencode → gptapi

The OpenAI API is stateless, so every request carries the full history.

| | Request 1 (new session) | Request 2 (follow-up) |
|---|---|---|
| `system` | 15,481 chars | the same 15,481 chars again |
| History | none | user (47), assistant "Axolotl" (7) |
| New user message | 47 chars | 53 chars |
| `tools` | 13 tools, 17,659 chars | the same 17,659 chars again |
| Other fields | `stream: true`, `stream_options.include_usage: true`, `max_completion_tokens: 32000` | same |
| Total | ~33k chars | ~33k, and it grows with every turn |

## 2. gptapi → chatgpt.com

| | Turn 1 | Turn 2 |
|---|---|---|
| Mapping | `new`: no known history | `matched`: the hash of the history before the last message matched turn 1's saved point |
| Typed into the page | 15,570 chars: `[System instructions]` + system prompt + `[Current message]` + message | **53 chars: only the new message** |
| Tools | not sent (gptapi 0.0.1 ignores `tools`) | not sent |
| Reply | "Axolotl", 5.2 s | "Axolotl", 2.1 s |

The heavy part crosses chatgpt.com once per conversation. After that, chatgpt.com's own
conversation memory holds the context, and gptapi types only the new message.

## 3. What is in opencode's system prompt (15,481 chars)

| Section | Chars | Useful for chatgpt.com? |
|---|---|---|
| Core: Harness, Communication, Intermediate Commentary, Final Answer, Working in codebases, Delegation, Destructive actions, Autonomy, Your Model | ~3,200 | partly |
| Code Mode plus Code Mode tool docs (for the `execute` tool) | 8,774 | no |
| `<available_skills>` | 921 | no |
| The user's `~/.claude/CLAUDE.md` (opencode reads it for Claude Code compatibility) | ~1,850 | no |
| `<env>`: working directory, platform, date | 735 | yes |

## 4. Other situations

| Situation | opencode sends | chatgpt.com gets | Evidence |
|---|---|---|---|
| Titles on (opencode's default) | an extra request with a title system prompt, in parallel with the first message | a separate new conversation (one more message) | observed 2026-11-24 |
| Next day, same session | the system prompt's date changes | `matched-nosystem`: the same conversation | mapping rule |
| Undo, edit or retry of an earlier message | history cut back to an earlier point | `branch`: a fresh tab continuing from the saved state of that point | mapping rule |
| opencode compaction (summary replaces old turns) | history that matches nothing | probably `new`: a new conversation with the flattened summary | mapping rule, not tested |
| gptapi or container restart | as usual | the same conversation in a fresh tab, from the saved state | tested 2026-11-24 |
| Image attached | image parts | `[image omitted]` | code |

## 5. Size limits that matter

- **Per message:** 32k characters were accepted (2026-11-25). The hard limit for a logged-out
  message is unknown.
- **Context window:** the logged-out window is unknown. One 2026 guide gives 27K tokens
  (~100k characters) for signed-in Free users. When the window is full, ChatGPT usually drops
  the **oldest** messages without saying so, and those are the instructions typed first.
- **gptapi's usage numbers:** gptapi reports `prompt_tokens` = characters typed this turn / 4
  (turn 2 above: 13 tokens). opencode uses these numbers to decide when to compact, so it
  sees an almost empty context forever and never compacts, while chatgpt.com may be dropping
  old turns.

## 6. Ways to keep the chatgpt.com side small

Options considered (decided 2026-11-25: 1, 2, 3 and 4 chosen; see `TOOL-CALLING.md` §5):

1. **Slim first message.** Replace the client's system prompt with gptapi's own short prompt
   plus the client's `<env>` block: ~1k instead of 15.5k.
2. **Compact tools** (with tool calling): an allowed-tool list, one line per tool (~1.4k), the
   protocol last, and a short reminder each turn.
3. **Cap tool results.** Long file reads and command output keep the head and tail plus a
   "truncated, use read with offset" note.
4. **Context budget and rollover.** gptapi counts the characters each chatgpt.com conversation
   holds. Over a budget it starts a fresh conversation seeded from the client's own history:
   the slim prompt plus the most recent turns that fit. This costs no extra message, because
   opencode already sends the full history. The instructions are fresh again, instead of being
   silently dropped.
5. **Honest usage numbers.** Report the estimated size of the chatgpt.com conversation as
   `prompt_tokens`, and set the model's `limit.context` in `opencode.json`, so opencode's
   context display, and its compaction if wanted, match reality.
6. **Measure first.** Spend a few messages to find the hard per-message limit and roughly
   where the context window starts forgetting.
