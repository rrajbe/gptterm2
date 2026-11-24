# chatgpt.com anonymous protocol (captured 2026-11-18, Chrome 156, qqqnews1, incognito context)

Logged-out chatgpt.com serves the "unauth-mweb" server-rendered app (worker version header 6e6c1500-...).

## Per turn
1. POST /unauth-mweb/sentinel/chat-requirements/prepare  {p: <browser-fingerprint PoW seed blob>}
   -> {prepare_token, turnstile:{required:true, dx:<obfuscated VM program ~30KB>},
       proofofwork:{required:true, seed, difficulty}, so:{collector_dx, snapshot_dx}}
2. Browser runs turnstile dx + PoW + "so" collectors (also POST /backend-api/sentinel/req).
3. POST /unauth-mweb/sentinel/chat-requirements/finalize {prepare_token, proofofwork, turnstile}
   -> {token, expire_after:540}
4. POST /unauth-mweb/conversation/prepare (form)  -> {conduit_token}
5. POST /unauth-mweb/conversation/updates?operationId=<uuid>   (form-urlencoded)
   fields: prompt, conversationState (JSON: backendConversationId, parentMessageId, userMessageCount),
           chatRequirementsToken, proofToken, turnstileToken, timingToken, userMessageId,
           assistantMessageId, conversationRetryOwner {"mode":"anonymous"}, oai-session-id, ...
   headers: x-conduit-token, OAI-Session-Id, X-Web-Mobile-* (worker version, affinity, protocol 2)
   response: application/vnd.openai.conversation-source+ndjson, one JSON object per line:
     control{kind: started|conversation-id|resume-token|assistant-content-started|last-token-received|terminal-received}
     content{messageId, revision, mode: replace|append, baseRevision, markdown, dil.fallbackMarkdown, status: streaming|complete}
     state{conversationState}  <- carry into next turn (parentMessageId etc.)
     shared{html}  (UI templates; ignore)
     end
Conversation URL: /uc/<conversationId>. Context carried across turns (verified).

## Session state
Cookies: oai-did (device id, 1y), oai-did-first-seen, oai-sc, __cf_bm, _cfuvid, __cflb, oai-mweb-route/origin.
localStorage: oai/apps/noAuthUserMessageCount (anon message cap counter).

## Implication
Every turn needs a fresh Turnstile + PoW + collector token computed by browser JS. A standalone
HTTP client would have to emulate Cloudflare Turnstile => not doing that. Design: keep a real
browser tab, let the page do sentinel, relay prompt in and ndjson stream out over CDP.

## Learned while building (2026-11-18)
- Composer is `textarea#mobile-composer-prompt` (name=prompt). Input.insertText + Enter submits.
- Each `content` event carries the whole answer so far in `dil.fallbackMarkdown` (not a strict
  prefix of the next one: escapes get re-rendered). `markdown` field stays "" in practice.
- Network.streamResourceContent on responseReceived gives the ndjson chunks live via dataReceived.
  The page aborts the request after `end` (loadingFailed ERR_ABORTED) — harmless.
- Anonymous chats cannot be reopened by URL: /uc/<id> redirects to / (same or new context).
  The chat lives in the page's memory. Grafting a saved `conversationState`
  ({backendConversationId, parentMessageId, userMessageCount}) into the first POST from a fresh
  page (CDP Fetch interception) continues the same conversation, context intact.
- A CDP browser context created with disposeOnDetach=false survives client disconnects.
