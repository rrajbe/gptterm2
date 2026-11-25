# gptapi

An OpenAI-compatible HTTP API (chat, with emulated tool calling) backed by **logged-out
chatgpt.com running in a real Chromium**, driven over the Chrome DevTools Protocol.

The page does its own Turnstile / proof-of-work / sentinel work. gptapi only types the prompt
into the composer and reads the answer stream off the network (`Network.streamResourceContent`
on `POST /unauth-mweb/conversation/updates`). There are no stealth tricks, no faked tokens and no
CAPTCHA solving: if a human check shows up, a person has to complete it (over noVNC in the
container).

## Quick start (Docker)

```sh
./install.sh --test                              # build, start, wait until healthy, one test message
curl -s http://127.0.0.1:8000/health            # browser connected?
open http://127.0.0.1:6080                       # watch the browser; click a human check if one appears
```

Setting it up on another computer, the opencode config and everyday commands:
[INSTALL.md](INSTALL.md).

The image (~1.2 GB; builds natively on arm64 and amd64) holds Debian Chromium headed on Xvfb,
noVNC on 6080, and gptapi on 8000; both ports are bound to 127.0.0.1. Data (Chromium profile,
state, transcripts in `/data/gptapi/sessions/*.jsonl`) lives in the `gptapi-data` volume and
survives restarts; a restarted container continues each conversation from saved state.

Tested 2026-11-24: Turnstile passed silently in the container (also with mitmproxy in the
path), curl stream/non-stream, history matching, opencode 2.4.0 `run` + `--continue`, and
continuing an opencode session after `docker restart`. Phase 1 evidence: `phase1/RESULTS.md`.

## Run modes

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # aiohttp + websocket-client
```

**Container / external browser (default).** Point gptapi at an already running Chromium with
remote debugging on:

```sh
CDP_URL=http://127.0.0.1:9222 .venv/bin/python -m gptapi
```

A docker service name works too (`CDP_URL=http://browser:9222`): gptapi sends `Host: localhost`
because Chrome refuses other Host headers. It also rewrites the browser's websocket URL to the
host and port it was given.

**Dev mode (macOS).** gptapi starts a local Chrome itself, with its own profile
(`~/Library/Application Support/gptapi-dev/chrome-profile`) on port 9335. It reuses that
Chrome if it is already running, and leaves it running on exit. In dev mode `CDP_URL` is
ignored, and port 9333 is refused.

```sh
GPTAPI_LAUNCH_BROWSER=1 .venv/bin/python -m gptapi
# close it later (only this Chrome):
.venv/bin/python -c "from gptapi.cdp import CDP; CDP('http://127.0.0.1:9335').call('Browser.close')"
```

Tests: `.venv/bin/python -m pytest -q tests` (pure parsing/mapping tests, plus server tests with a
fake browser; no network).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `GPTAPI_HOST` | `127.0.0.1` | listen address (`0.0.0.0` in a container) |
| `GPTAPI_PORT` | `8000` | listen port |
| `CDP_URL` | `http://127.0.0.1:9222` | browser DevTools endpoint (`http://` or a `ws://` browser URL) |
| `GPTAPI_LAUNCH_BROWSER` | off | `1` = dev mode, launch a local Chrome on `GPTAPI_DEV_PORT` |
| `GPTAPI_DEV_PORT` | `9335` | dev-mode Chrome debugging port (9333 refused) |
| `GPTAPI_BROWSER` | auto | path to the Chrome/Chromium binary for dev mode |
| `GPTAPI_DATA` | `./data` (`/data` in the container) | state, transcripts, debug dumps, log |
| `GPTAPI_KEY` | none | if set, `/v1/*` requires `Authorization: Bearer <key>` |
| `GPTAPI_MAX_TABS` | `4` | max open conversation tabs (LRU-closed, re-opened by grafting) |
| `GPTAPI_NOVNC_URL` | `http://localhost:6080` | URL shown in human-check errors |
| `GPTAPI_RAW` | off | `1` = also append the raw ndjson stream to `data/raw/YYYYMMDD.ndjson` |
| `GPTAPI_LOG_LEVEL` | `INFO` | log level (log also goes to `data/gptapi.log`) |
| `GPTAPI_AGENT_MODE` | `1` | `0` = ignore `tools` (plain chat, client system prompt passed through) |
| `GPTAPI_TOOLS` | `edit,write,patch,apply_patch,glob,grep,read,list,shell` | tools offered to chatgpt.com (others are left out); `*` = all the client sends |
| `GPTAPI_TOOL_RESULT_CHARS` | `8000` | longer tool results are cut to the first 75% and last 25% with a "use read with offset/limit or grep" note |
| `GPTAPI_ROLLOVER_CHARS` | `70000` | past this many characters in one chatgpt.com conversation, the next turn starts a fresh one |

## Endpoints

- `GET /v1/models`: `chatgpt-web`, plus `chatgpt-web/<name>` for every named conversation.
- `POST /v1/chat/completions` (also `/chat/completions`): `stream: false` returns a
  `chat.completion`. `stream: true` returns SSE `chat.completion.chunk`s ending with
  `data: [DONE]`, and honours `stream_options.include_usage`. Usage numbers are estimates
  (characters / 4). Any `model` value is accepted. Only `chatgpt-web/<name>` has a special
  meaning. `tools` switch on agent mode (see "Tool calling"); `tool_choice`, `temperature`
  and similar fields are ignored.
- `GET /health`: browser connection, open tabs (URL, busy, human check) and human-check
  state. Returns 200 when usable and 503 otherwise. It needs no API key.

Every completion response has an `X-Conversation-Id` header (the chatgpt.com conversation id).
The completion `id` is `chatcmpl-<turn>-<conversation id>`. `X-Gptapi-Mapping` says how the
request was mapped (`new`, `matched`, `matched-nosystem`, `branch`, `named`, `pinned`).

### Streaming deltas

chatgpt.com sometimes rewrites text it has already streamed, so gptapi only emits text that is
stable. The answer is split into markdown blocks on blank lines outside ``` fences, and a block is
sent once a later block has started. The last block is sent at the end. Nothing is ever retracted.
Deltas therefore arrive a paragraph or code block at a time, not token by token. Non-streaming
responses return the fullest text seen. chatgpt.com's `fallbackMarkdown` backslash-escapes
punctuation (`Paris\!`). gptapi removes the escapes that never change rendering, outside code,
and keeps `\*`, `\_`, `` \` ``, `\#`, a leading `1\.`, and the like.

### Errors

| Situation | HTTP | `error.type` |
|---|---|---|
| logged-out message cap, sign-in wall, message not accepted, upstream non-200 | 429 | `rate_limit_exceeded` (message = page text) |
| chatgpt.com refused the message as too long (HTTP 400; ~120k characters is accepted, 180k is not) | 400 | `context_length_exceeded` |
| visible human check (Turnstile challenge, "verify you are human", CF interstitial) for more than 20 s | 503 | `human_check_required`: "open noVNC at http://localhost:6080 and complete the check" |
| browser unreachable or connection lost (reconnects on the next request) | 503 | `browser_unavailable` |
| bad request body | 400 | `invalid_request_error` |
| wrong or missing API key | 401 | `invalid_api_key` |

Every failure saves a screenshot and the page text (dialogs, body tail, URL) to
`GPTAPI_DATA/debug/<time>-<tag>.{png,json}`. If an error happens after an SSE stream has
started, it is sent as a `data: {"error": ...}` event before `[DONE]`.

## Conversation mapping

The internal key is the chatgpt.com conversation id. Each request is resolved in this order:

1. **Pinned.** An `X-Conversation-Id` request header with a known conversation id continues that
   conversation. Any other header value is treated as a **name**, and so is the model
   `chatgpt-web/<name>`. A name maps to a conversation id in the state file. Once a named
   conversation exists, only the last message is sent. If it does not exist yet, it is created
   from the flattened history and the name is bound to it.
2. **Automatic.** gptapi hashes the history before the last message (system + all earlier
   messages; whitespace is collapsed and text-less tool-call messages are dropped). After every
   turn it stores the hash of "history + this answer" for that conversation. If an incoming
   prefix matches, only the new last message is sent. If the exact hash misses, a hash that
   ignores system messages is tried, because some clients put the date in the system prompt. A
   prefix must contain at least one assistant reply to be matched. If the match is an older point
   of a conversation (the client regenerated or edited a message), gptapi **branches**: it grafts
   that point's `conversationState` into a fresh page.
3. **New.** Otherwise gptapi starts a new chatgpt.com conversation. Its first prompt is the
   flattened history: `[System instructions]`, then `[Conversation so far]` (with `User:` /
   `Assistant:` labels), then `[Current message]`. A lone user message is sent as is.

### Tabs and state

- There is one private browser context (`Target.createBrowserContext`, `disposeOnDetach=false`;
  cookies are saved and restored) and one tab per active conversation. Requests to the same
  conversation are serialized with an asyncio lock. Different conversations run in parallel, up
  to `GPTAPI_MAX_TABS`. When the cap is reached, the least recently used idle tab is closed.
- Anonymous chats cannot be reopened by URL (`/uc/<id>` redirects home), because they live in
  page memory. A conversation whose tab is gone is continued in a fresh tab. gptapi intercepts
  that tab's first `conversation/updates` POST (CDP Fetch) and grafts the saved
  `conversationState` into it. Context survives this (verified live).
- If gptapi restarts while the browser keeps running, it re-attaches to a tab that is still on
  `/uc/<id>`.
- `GPTAPI_DATA/state.json` holds the context id, cookies, names, and for each conversation its
  latest `conversationState`, tab id and history points.
- `GPTAPI_DATA/sessions/<conversation_id>.jsonl` holds the transcripts, in the same records as
  gptterm. The user line is written as soon as the conversation id is known, and the assistant
  line on every ending, with `incomplete`/`reason` and `retracted`/`final_text` flags. While an
  answer streams, a snapshot is written to `inflight/<turn>.json` every 2 s and recovered into
  the transcript on the next start if the process died.

## opencode

Use [`opencode.json`](opencode.json) (explained entry by entry in [INSTALL.md](INSTALL.md#4-opencode)).
It adds the `gptapi` provider (`@ai-sdk/openai-compatible`, base URL
`http://127.0.0.1:8000/v1`, model `chatgpt-web`), makes it the default, and turns off
opencode's title generation.

opencode resends the full history on every turn, so automatic mapping keeps one chatgpt.com
conversation per opencode session. Side requests with a different system prompt, such as
title generation, each start a separate new chatgpt.com conversation and count against the
logged-out message cap. That's why the config turns titles off. To pin a session to a named
conversation, add a model `chatgpt-web/<name>`. opencode's tools work through gptapi's emulated
tool calling (below). Because the model is called `chatgpt-web`, opencode offers its `patch`
tool for edits instead of `edit`/`write`; gptapi sends `patch`'s full format description.

## Tool calling (agent mode)

chatgpt.com has no client tool calling, so when a request carries OpenAI `tools`, gptapi
emulates it. The design and the measurements behind it are in `research/TOOL-CALLING.md`,
`research/LIMITS.md` and `research/OPENCODE-FLOW.md`.

- **Lean first message.** The client's system prompt is replaced by a short coding-agent prompt
  plus the client's `<env>` block. Only the allowed tools (`GPTAPI_TOOLS`) are listed, one line
  each. The tool protocol comes last. opencode's first message drops from ~15.5k characters to
  ~2.5–3.9k.
- **Protocol.** The model replies with fenced ```` ```tool_call ```` blocks holding
  `{"name", "arguments"}` JSON. gptapi checks each call against the tool's schema (unknown
  tools and missing required arguments stay as visible text; unknown argument names are
  dropped) and returns `tool_calls` with `finish_reason: "tool_calls"`. When streaming, text
  before the first block streams as usual and the calls arrive in one chunk at the end.
- **Later turns.** Tool results are typed as `[tool result <name>]` text, several results of one
  step in one message, plus a one-line reminder of the protocol. Each agent step is one
  chatgpt.com message.
- **Large results** are cut to `GPTAPI_TOOL_RESULT_CHARS` with a note telling the model to page
  with `read` offset/limit or `grep`.
- **Rollover.** chatgpt.com keeps about 24–28k tokens (~100k characters of real content) and
  silently drops the oldest messages: the instructions and the task. Past
  `GPTAPI_ROLLOVER_CHARS`, gptapi starts a fresh conversation in the same request, seeded with the
  instructions, the original task, the most recent turns that fit in half the budget, and the
  protocol (`X-Gptapi-Mapping: rollover`). A conversation that rolled over forwards to its
  successor, also for pinned ids and names. A conversation with only one turn is never rolled
  over again. Rollover also applies to plain chat.

Piloted 2026-11-25 with opencode 2.4.0. "What does calc.py do?" took 2 chatgpt.com messages
(read, then answer). "Add sub(), add a test, run the tests" took 3 (one patch to two files, one
shell call, then the summary); the edits were correct.

## Limits

- Agent steps cost logged-out messages: each tool round trip is one chatgpt.com message.

- One tab sends at a time. chatgpt.com only sends from the visible tab, so gptapi brings a tab
  to the front, types and sends under a lock that is released as soon as the request has left
  (about 1-2 s); answers from several conversations still stream at the same time. If the
  prompt is still in the box 3 s after Enter, gptapi clicks Send, then presses Enter once more.

- Logged-out chatgpt.com has a small daily message cap. When it is hit, requests return 429.
- Each request is one browser turn, so `n > 1`, `logprobs`, `temperature` and `max_tokens` have
  no effect.
- Images in messages are replaced with `[image omitted]`.
