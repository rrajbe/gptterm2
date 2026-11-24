# Phase 1 results: chatgpt.com logged-out in the gptapi-browser container (2026-11-24)

Container: `gptapi-browser:dev` (from `docker/`), run with `docker compose up -d` (service `gptapi`).
Driver: `phase1/p1test.py`. It reuses `cdp.py` and gptterm's `Tab`/`Turn`: a private browser context,
`Input.insertText` + Enter, and the ndjson read through `Network.streamResourceContent`.
Messages sent to chatgpt.com: 3 in total (A: 2, B: 1).

## Summary table

| # | Check | Result | Evidence |
|---|---|---|---|
| A.1 | chatgpt.com loads, headed Chromium in Docker | OK. The composer was ready 1.4 s after navigate. Title "ChatGPT: Chat, Work, Create & Code with AI" | `results/A-01-loaded.png`, `A-result.json` (page_state) |
| A.2 | Turnstile / Cloudflare | **Passed silently.** No interstitial, no visible challenge iframe, no request to `challenges.cloudflare.com`. All sentinel calls (`chat-requirements/prepare` and `/finalize`, `backend-api/sentinel/req`, `sentinel/ping`) returned 200 | `A-result.json` (network_interesting, network_errors) |
| A.3 | Turn 1 "Pick a random fruit…" | Answer "Mango". POST sent 1.2 s after Enter. **TTFT 2.6 s**, total 3.9 s, HTTP 200 | `A-02-after-turn1.png`, `A-stream.ndjson` |
| A.4 | Turn 2 "Which fruit did you just name?" (proves context) | "You named Mango. It is known as the 'king of fruits'…". **TTFT 1.8 s**, total 7.8 s | `A-03-after-turn2.png` |
| A.5 | conversationId | `control` kind `conversation-id` gave `6b05ac44-6f34-83ea-b96a-9f83729ac3aa`, the same id on both turns. The page URL became `https://chatgpt.com/uc/6b05ac44-6f34-83ea-b96a-9f83729ac3aa`. The `state` event's `userMessageCount` went 1 → 2 and `parentMessageId` advanced | `A-result.json` turns[].conversation_state |
| B.1 | mitmdump as upstream proxy for a second Chromium (own profile `/tmp/chrome-mitm`, CDP :9223, `--proxy-server`, `--ignore-certificate-errors` on that instance only) | OK. 440 decrypted flows, all to chatgpt.com, all over HTTP/2 | `results/B-flows.mitm` (6.7 MB), `B-mitm-requests.jsonl` |
| B.2 | Turnstile / Cloudflare with mitm in the path | **No difference observed.** No challenge, no 403, no `cf-mitigated` header, no status ≥ 400. The page loaded in 1.4 s and the turn returned HTTP 200 with answer "Mars" | `B-01-loaded.png`, `B-02-after-turn1.png`, `B-mitm-requests.jsonl` |
| B.3 | Turn through mitm | POST sent 1.2 s after Enter. TTFT 4.1 s, total 5.8 s. The mitm addon streams the ndjson through (tee), and the tee copy is byte-identical to the CDP capture (11029 B each) | `B-mitm-stream.ndjson` == `B-stream.ndjson`, `B-result.json` |
| B.4 | Where the conversation ids live | See the next section | `B-mitm-requests.jsonl` (form field), `B-id-locations.txt` |
| C.1 | Chromium version | Debian `chromium` 156.0.8078.108 (trixie). The UA string says "X11; Linux x86_64" even on arm64 | `A-result.json` browser/user_agent |
| C.2 | Image size | 1.18 GB (`docker image inspect` reports 1,178,083,541 B) | |
| C.3 | Container RAM (docker stats, 8 GB Docker VM) | Idle, fresh start, about:blank: ~0.78 GiB. Answering in A (one chatgpt.com tab): peak 1.05 GiB, ~1.0 GiB afterwards. Before B: 0.53 GiB. B (second Chromium + mitmdump): peak 0.97 GiB. Idle after B with the chatgpt tab still open: 0.47 GiB (cgroup anon ≈ 376 MiB). CPU spikes to ~200% for ~1 s while the page computes sentinel/PoW | `A-docker-stats.txt`, `B-docker-stats.txt` |

## Conversation id / parent message id: where they show up (decrypted traffic, test B)

1. **Stream, `control` event**, kind `conversation-id`: `conversationId` plus `canonicalPath: /uc/<id>`.
   The `resume-token` and `terminal-received` control events also carry `conversationId`.
   `assistant-content-started` and `terminal-received` carry the assistant `messageId`.
2. **Stream, `state` event**: `conversationState` (a JSON string) =
   `{"backendConversationId":"<convId>","messages":[],"parentMessageId":"<assistant messageId>","userMessageCount":1}`.
   It is sent twice per turn; the second copy adds `safety.dismissedInterventionIds`.
3. **Request form field `conversationState`** on POST `/unauth-mweb/conversation/updates`.
   First turn: `{"messages":[],"parentMessageId":"client-created-root","safety":{...},"userMessageCount":0}`.
   It has no backendConversationId yet.
4. **After the turn, the page pre-warms the next turn** with POST `/unauth-mweb/conversation/prepare`.
   Its form `conversationState` already holds `backendConversationId=<convId>` and
   `parentMessageId=<last assistant messageId>`, which is exactly the last `state` event.
   This confirms that the client carries the `state` event into the next request.
5. Other places it leaks (telemetry only): `Referer: https://chatgpt.com/uc/<id>` on later requests,
   the `events/page-view` body (`url`), and `ces/v1/t` bodies (`conversation.id`, `conversationId`).
   The literal snake-case `conversation_id` appears only inside JS bundles, never in this traffic.
6. Other form fields seen: `prompt`, `userMessageId`, `assistantMessageId` (`pending-<uuid>`),
   `chatRequirementsToken` (~2.9 KB), `proofToken` (~0.8 KB), `turnstileToken` (~1.5 KB), `timingToken`,
   `conversationRetryOwner {"mode":"anonymous"}`, `oai-session-id`, `clientContextualInfo`, `timezone`.
   Headers seen: `x-web-mobile-*` (worker version, document affinity, protocol 2, renderer "octane"),
   `x-oai-turn-trace-id`.
7. The `turn-latency` control event reports `plan_type: guest`, `requested_model_experience: instant`,
   `user_agent: web_desktop` (the desktop layout was served this time, not the mobile composer).

The "turnstile" in this flow is OpenAI's own sentinel VM program (`turnstile.dx` in
`chat-requirements/prepare`), not the Cloudflare widget. Cloudflare here is only the CDN/WAF
(`__cf_bm`, `_cfuvid`, `__cflb` cookies). It did not object to mitmproxy's re-originated TLS
(the TLS fingerprint was not Chrome's).

## Problems found and fixes

| # | Problem | Resolution |
|---|---|---|
| 1 | Chromium would not start: "No usable sandbox". Docker's default seccomp profile blocks unprivileged user namespaces. The setuid `chromium-sandbox` does not work either (no CAP_SYS_ADMIN) | `compose.yaml` sets `security_opt: seccomp=unconfined`, which keeps the **Chromium sandbox on**. `start-chromium` checks with `unshare -Ur` and falls back to `--no-sandbox` (with a log warning) if namespaces are unavailable |
| 2 | A crash or new container leaves a stale `SingletonLock` in the profile, which blocks the restart | `start-chromium` removes `Singleton*` before launching. A `kill -9` of Chromium was tested: supervisord restarted it and CDP was back within ~10 s |
| 3 | `pip install mitmproxy` backtracked for minutes over sdists | `pip install --only-binary=:all: mitmproxy` installs 12.3.1 in seconds (arm64 wheels) |
| 4 | Telemetry beacons (`/unauth-mweb/events/*`) show up as `net::ERR_ABORTED` in CDP while mitm saw them return 204/200 | Harmless (keepalive/beacon). Ignore them in the API server |
| 5 | Debian's wrapper `/etc/chromium.d/*` adds its own flags (`--enable-gpu-rasterization`, `--disable-pings`, ...) and Debian's packaged Google API keys | Informational only |

No visible human check appeared in any test, so nothing is waiting for a human to click.

## Files

- `phase1/p1test.py`: test driver (A and B), run inside the container.
- `phase1/run_b.sh`: test B: starts mitmdump and the second Chromium, runs one turn, then stops both by PID.
- `phase1/mitm_capture.py`: mitmdump addon (streams and tees the ndjson, logs requests and the decoded form).
- `phase1/mitm_grep.py`: searches a flow file for ids (`results/B-id-locations.txt`).
- `phase1/results/`: screenshots `A-01..03*.png` and `B-01..02*.png`; `A-/B-result.json`; `A-/B-stream.ndjson`;
  `B-flows.mitm`; `B-mitm-requests.jsonl`; `B-mitm-stream.ndjson`; `A-/B-docker-stats.txt`.

The mitmproxy venv (`/tmp/mitmvenv`) exists only in the stopped test container's writable layer. It is not in the image.
