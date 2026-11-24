"""aiohttp server: OpenAI-compatible /v1/chat/completions on top of chatgpt.com tabs."""
import asyncio
import json
import logging
import time
import uuid

from aiohttp import web

from . import __version__
from .browser import Browser, BrowserLost, GptapiError, HumanCheckRequired
from .mapping import MODEL_BASE, BadRequest, after_turn_hashes, resolve
from .store import Store
from .stream import StableEmitter

log = logging.getLogger("gptapi")

KEEPALIVE_S = 15
_appkey = getattr(web, "AppKey", None)
APP_KEY = _appkey("gptapi", object) if _appkey else "gptapi"
JOBS_KEY = _appkey("jobs", set) if _appkey else "jobs"


def err(status, etype, message, headers=None):
    return web.json_response({"error": {"type": etype, "message": message, "code": etype}}, status=status,
                             headers=headers)


def est_tokens(s):
    return max(1, len(s or "") // 4)


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.data)
        self.browser = Browser(cfg, self.store)
        self.tools_logged = False

    # ------------------------------------------------------------ plumbing
    def error_for(self, e, headers=None):
        if isinstance(e, HumanCheckRequired):
            return err(503, "human_check_required",
                       f"open noVNC at {self.cfg.novnc_url} and complete the check", headers)
        if isinstance(e, BrowserLost):
            return err(503, "browser_unavailable", str(e), headers)
        if isinstance(e, GptapiError):
            return err(e.status, e.etype, str(e), headers)
        log.exception("unexpected error", exc_info=e)
        return err(500, "internal_error", f"{type(e).__name__}: {e}", headers)

    @web.middleware
    async def auth(self, request, handler):
        if self.cfg.api_key and request.path.startswith("/v1/"):
            if request.headers.get("Authorization", "") != f"Bearer {self.cfg.api_key}":
                return err(401, "invalid_api_key", "missing or wrong bearer API key")
        return await handler(request)

    # ------------------------------------------------------------ endpoints
    async def models(self, request):
        now = int(time.time())
        ids = [MODEL_BASE] + [f"{MODEL_BASE}/{n}" for n in sorted(self.store.names())]
        return web.json_response({"object": "list", "data": [
            {"id": i, "object": "model", "created": now, "owned_by": "chatgpt.com"} for i in ids]})

    async def model(self, request):
        mid = request.match_info["mid"]
        if mid != MODEL_BASE and not mid.startswith(MODEL_BASE + "/"):
            return err(404, "model_not_found", f"unknown model {mid}")
        return web.json_response({"id": mid, "object": "model", "created": int(time.time()), "owned_by": "chatgpt.com"})

    async def health(self, request):
        try:
            await self.browser.ensure()
        except Exception:
            pass
        info, tabs = await self.browser.health()
        hc = dict(self.browser.human)
        if hc["required"]:
            hc["action"] = f"open noVNC at {self.cfg.novnc_url} and complete the check"
        ok = info["connected"] and not hc["required"]
        return web.json_response({"ok": ok, "version": __version__, "browser": info, "tabs": tabs,
                                  "max_tabs": self.cfg.max_tabs, "human_check": hc,
                                  "conversations": len(self.store.get("conversations") or {})},
                                 status=200 if ok else 503)

    async def chat(self, request):
        try:
            body = await request.json()
        except Exception:
            return err(400, "invalid_request_error", "body must be JSON")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return err(400, "invalid_request_error", "'messages' must be a list")
        if (body.get("tools") or body.get("tool_choice") or body.get("functions")) and not self.tools_logged:
            log.info("request carries tools/tool_choice; ignored (tool calling not supported yet)")
            self.tools_logged = True
        model = body.get("model") or MODEL_BASE
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        try:
            plan = resolve(self.store, body["messages"], model, request.headers.get("X-Conversation-Id"))
        except BadRequest as e:
            return err(400, "invalid_request_error", str(e))
        log.info("chat: mode=%s conv=%s stream=%s prompt=%d chars", plan.mode, plan.conv_id, stream, len(plan.prompt))

        loop = asyncio.get_running_loop()
        q = asyncio.Queue()

        def on_event(kind, val):
            loop.call_soon_threadsafe(q.put_nowait, (kind, val))

        async def job():
            # runs to completion even if the client goes away, so the transcript/state stay right
            try:
                res = await self.browser.turn(plan.prompt, plan.conv_id, plan.inject, on_event)
            except Exception as e:
                q.put_nowait(("error", e))
                return None
            cid = res["conversation_id"]
            self.store.add_point(cid, after_turn_hashes(plan.prefix, plan.last, [res["answer"]]), res["conv_state"])
            if plan.name:
                self.store.bind_name(plan.name, cid)
            q.put_nowait(("done", res))
            return res

        task = asyncio.ensure_future(job())
        request.app[JOBS_KEY].add(task)
        task.add_done_callback(request.app[JOBS_KEY].discard)
        turn_id = uuid.uuid4().hex[:8]
        created = int(time.time())

        if not stream:
            while True:
                kind, val = await q.get()
                if kind == "error":
                    return self.error_for(val)
                if kind == "done":
                    break
            res = val
            cid = res["conversation_id"]
            pt, ct = est_tokens(plan.prompt), est_tokens(res["answer"])
            return web.json_response({
                "id": f"chatcmpl-{turn_id}-{cid}", "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": res["answer"]},
                             "finish_reason": "stop" if not res["incomplete_reason"] else "length"}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
            }, headers={"X-Conversation-Id": cid, "X-Gptapi-Mapping": plan.mode})

        # ---- streaming: hold the headers until the conversation id (or an error) is known
        cid, pending = plan.conv_id, []
        while True:
            kind, val = await q.get()
            if kind == "error":
                return self.error_for(val)
            pending.append((kind, val))
            if kind == "conv":
                cid = val
            if kind in ("conv", "done") or (kind == "text" and cid):
                break
        if pending[-1][0] == "done":
            cid = pending[-1][1]["conversation_id"]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Conversation-Id": cid or "", "X-Gptapi-Mapping": plan.mode})
        await resp.prepare(request)
        cmpl_id = f"chatcmpl-{turn_id}-{cid}"

        async def send(delta=None, finish=None, usage=None, raw=None):
            if raw is not None:
                obj = raw
            else:
                obj = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
                if usage:
                    obj["usage"] = usage
            await resp.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n")

        emitter = StableEmitter()
        await send({"role": "assistant", "content": ""})
        res = None
        try:
            while True:
                if pending:
                    kind, val = pending.pop(0)
                else:
                    try:
                        kind, val = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        await resp.write(b": keepalive\n\n")
                        continue
                if kind == "text":
                    d = emitter.feed(val)
                    if d:
                        await send({"content": d})
                elif kind == "error":
                    e = val
                    etype = getattr(e, "etype", "internal_error")
                    msg = (f"open noVNC at {self.cfg.novnc_url} and complete the check"
                           if isinstance(e, HumanCheckRequired) else str(e))
                    await send(raw={"error": {"type": etype, "message": msg, "code": etype}})
                    break
                elif kind == "done":
                    res = val
                    d = emitter.finish(res["answer"])
                    if d:
                        await send({"content": d})
                    await send({}, finish="stop" if not res["incomplete_reason"] else "length")
                    if include_usage:
                        pt, ct = est_tokens(plan.prompt), est_tokens(emitter.sent)
                        await send(usage={"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct})
                    break
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            log.info("client went away mid-stream; turn continues in the background")
            raise
        finally:
            if res and emitter.sent and emitter.sent != res["answer"]:
                # the client kept what we streamed; remember that variant of the history too
                self.store.add_point(res["conversation_id"],
                                     after_turn_hashes(plan.prefix, plan.last, [emitter.sent]),
                                     res["conv_state"], count=False)
        return resp


def build_app(cfg):
    a = App(cfg)
    app = web.Application(middlewares=[a.auth], client_max_size=32 << 20)
    app[APP_KEY] = a
    app[JOBS_KEY] = set()
    app.router.add_get("/health", a.health)
    app.router.add_get("/v1/models", a.models)
    app.router.add_get("/v1/models/{mid:.+}", a.model)
    app.router.add_post("/v1/chat/completions", a.chat)
    app.router.add_post("/chat/completions", a.chat)

    async def on_startup(app):
        a.store.recover_inflight()
        try:
            await a.browser.ensure()
        except Exception as e:
            log.warning("browser not reachable yet (%s); will retry on first request", e)

    async def on_cleanup(app):
        jobs = list(app[JOBS_KEY])
        if jobs:
            log.info("waiting up to 30 s for %d running turn(s)", len(jobs))
            await asyncio.wait(jobs, timeout=30)
        a.browser.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
