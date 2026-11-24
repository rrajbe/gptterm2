# Changelog

## 0.0.1 (2026-11-24)

First release.
- OpenAI-compatible `/v1/chat/completions` (streaming and non-streaming) and `/v1/models`,
  backed by logged-out chatgpt.com in headed Chromium inside one Docker container.
- One chatgpt.com conversation per client conversation (history matching, or pinned by
  `X-Conversation-Id` / `chatgpt-web/<name>`); conversations survive restarts.
- noVNC on port 6080 for clearing human checks by hand.
- `install.sh` and `INSTALL.md` for setting up on another computer; clean `opencode.json`.
