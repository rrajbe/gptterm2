# Changelog

## 0.0.2 (2026-11-25)

- Emulated tool calling (agent mode) for requests with `tools`: lean first message (client
  system prompt replaced, allowed tools one line each, protocol last), ```tool_call parsing
  with schema checks, `tool_calls` responses (streaming and not), a per-turn reminder.
- Large tool results are cut with a paging note (`GPTAPI_TOOL_RESULT_CHARS`).
- Rollover to a fresh chatgpt.com conversation past `GPTAPI_ROLLOVER_CHARS` (70k).
- Cookie banner: "Reject non-essential" is clicked; a prompt it swallowed is retyped once.
- Oversized messages return 400 `context_length_exceeded` instead of 429.
- The model id is now `chat-web` (`chatgpt-web` still works). For a model id containing "gpt",
  opencode offers its `patch` tool, and chatgpt.com's model then reaches for its own built-in
  apply-patch tool and gives up. Rename the model in existing opencode configs.
- A short answer claiming the tools aren't connected gets one nudge in the same conversation.
  Agent-mode text is held until it is 1,500 characters long, so the client never sees such a
  refusal.
- Rollover keeps the latest turn (shortened if needed) next to the current tool result.
- `docker/.env` now passes gptapi settings (`GPTAPI_TOOLS`, `GPTAPI_ROLLOVER_CHARS`, ...)
  into the container.

## 0.0.1 (2026-11-24)

First release.
- OpenAI-compatible `/v1/chat/completions` (streaming and non-streaming) and `/v1/models`,
  backed by logged-out chatgpt.com in headed Chromium inside one Docker container.
- One chatgpt.com conversation per client conversation (history matching, or pinned by
  `X-Conversation-Id` / `chatgpt-web/<name>`); conversations survive restarts.
- noVNC on port 6080 for clearing human checks by hand.
- `install.sh` and `INSTALL.md` for setting up on another computer; clean `opencode.json`.
