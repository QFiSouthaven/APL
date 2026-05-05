"""FastAPI app: serves the desktop UI and exposes REST + WebSocket control surface."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from . import activity as activity_module
from .charlie.workspace import CharlieWorkspace, SandboxError, get_current as charlie_get_current
from .code_review import review_with_dialogue
from .config import STATE_FILE, STATIC_DIR, ensure_dirs
from .discovery import get_all_peers
from .health import probe
from .lm_client import LMLinkClient
from .monitoring import ErrorEvent, ErrorMonitor, install_asyncio_exception_handler
from .orchestrator import (
    AgentConfig,
    CharlieConfig,
    Orchestrator,
    RunConfig,
    STATUS_RUNNING,
    STATUS_PAUSED,
)
from .sessions import PresetStore, SessionStore
from .storage import SafeStorage
from . import user_config

logger = logging.getLogger(__name__)


class WSHub:
    """Broadcasts JSON events to all connected websockets. Serializes per-socket sends."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._locks: dict[WebSocket, asyncio.Lock] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.add(ws)
        self._locks[ws] = asyncio.Lock()

    def disconnect(self, ws: WebSocket) -> None:
        self._sockets.discard(ws)
        self._locks.pop(ws, None)

    async def broadcast(self, event_type: str, **fields: Any) -> None:
        # NOTE: parameter is `event_type`, not `event`, so callers can pass an
        # `event=...` field in the payload (e.g. `error_logged` carries the
        # ErrorEvent dict under that key) without colliding with the positional.
        payload = json.dumps({"type": event_type, **fields}, default=str)
        dead: list[WebSocket] = []
        for ws in list(self._sockets):
            lock = self._locks.get(ws)
            if lock is None:
                continue
            try:
                async with lock:
                    await ws.send_text(payload)
            except (WebSocketDisconnect, RuntimeError):
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# ── Pydantic request models ─────────────────────────────────────────────────


class AgentBody(BaseModel):
    name: str
    model: str
    persona: str = ""


class CharlieBody(BaseModel):
    enabled: bool = False
    model: str = ""


class StartRunBody(BaseModel):
    theme: str = ""
    agents: list[AgentBody] = Field(min_length=2, max_length=4)
    loop_limit: int = 3
    pause_after_each_turn: bool = False
    auto_retry: int = 0
    auto_retry_backoff_s: float = 2.0
    charlie: CharlieBody = Field(default_factory=CharlieBody)
    intel_collab_directive: bool = True
    intel_anti_rambling: bool = True
    intel_anti_yes_man: bool = True
    intel_redundancy_threshold: float = 0.7
    intel_brief_threshold_tokens: int = 30
    intel_agreement_threshold: int = 2


class ResumeBody(BaseModel):
    injection: str | None = None


class ChoiceBody(BaseModel):
    action: str
    note: str | None = None


class PresetBody(BaseModel):
    name: str
    config: dict


class PresetUpdateBody(BaseModel):
    name: str | None = None
    config: dict | None = None


class SummarizeBody(BaseModel):
    # Defined at MODULE level (not inside create_app) so FastAPI can introspect
    # it as a Pydantic body. When this lived inside the factory, FastAPI fell
    # back to treating `body` as a query parameter and EVERY POST returned 422
    # with `{"detail":[{loc:["query","body"], msg:"Field required", ...}]}` —
    # which the frontend rendered as the infamous "Object: Object: error" toast.
    model: str | None = None
    session_id: str | None = None  # historical session to re-summarize


class PersonaHandoff(BaseModel):
    # Wire format is fixed across siblings (prompt-enhancer posts EXACTLY this).
    # See round-robin docs / prompt-enhancer's handoff client. Do not rename
    # fields without coordinating both sides.
    theme: str
    alpha_persona: str = ""
    bravo_persona: str = ""
    source: str = "prompt-enhancer"


class ReviewRequest(BaseModel):
    # Contract: matches what development.reviewers.RoundRobinReviewer POSTs.
    # See `src/round_robin/code_review.py` for the response shape.
    layer: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    files: dict[str, str] = Field(min_length=1)
    model: str | None = None  # optional: override which LM Studio model the dialogue uses


# ── App factory ─────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    ensure_dirs()
    hub = WSHub()
    client = LMLinkClient()
    presets = PresetStore()
    sessions = SessionStore()
    monitor = ErrorMonitor()

    async def _broadcast_error(event: ErrorEvent) -> None:
        # Defensive: a crash here would be re-recorded by the asyncio loop's
        # exception handler, which would spawn another _broadcast_error task —
        # cascading until the error log fills the disk. Swallow + log instead.
        try:
            await hub.broadcast("error_logged", event=asdict(event))
        except Exception:
            logger.exception("error_logged broadcast failed; not re-recording")

    monitor.set_broadcast(_broadcast_error)

    async def emit(event: str, **fields: Any) -> None:
        # Cross-umbrella activity feed: stamp the ring buffer first so a
        # later broadcast crash never loses the event from /api/activity.
        # record_emit is best-effort (swallows exceptions internally).
        activity_module.record_emit(event, fields)
        # Auto-capture any '*_error' event into the monitor before broadcasting.
        if event.endswith("_error") and "error" in fields:
            category = event[:-len("_error")] or "system"
            monitor.record(category, fields.get("error") or "(no message)", **{
                k: v for k, v in fields.items() if k != "error"
            })
        elif event == "agent_error":
            monitor.record(
                "agent",
                fields.get("message") or fields.get("error") or "(no message)",
                turn=fields.get("turn"),
                agent_name=fields.get("agent_name"),
                error_class=fields.get("error_class"),
                auto_retry=fields.get("auto_retry", False),
            )
        await hub.broadcast(event, **fields)
        if event == "run_done":
            try:
                sessions.save(orch.public_state())
            except Exception as exc:
                logger.exception("session save failed")
                monitor.record("system", f"Session save failed: {exc}")

    orch = Orchestrator(client, emit)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        install_asyncio_exception_handler(monitor)
        try:
            yield
        finally:
            await orch.stop()
            await client.aclose()

    app = FastAPI(title="Round Robin", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Cache-busting token: rewritten on every server boot so the browser
    # always re-fetches static assets after the dev rebuilds them.
    _ASSET_VERSION = str(int(time.time()))

    @app.get("/")
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace('href="/static/app.css"', f'href="/static/app.css?v={_ASSET_VERSION}"')
        html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_ASSET_VERSION}"')
        # The index itself should never cache either.
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ── Health & models ────────────────────────────────────────────────────

    @app.get("/api/health")
    async def get_health() -> JSONResponse:
        # Existing UI consumes `reachable`, `models`, `link`, `local_device`,
        # `error`. v1.2.0 of prompt-enhancer expects `status`/`service`/
        # `version` for service introspection — additive merge keeps both
        # consumers happy without breaking the existing app.
        body = await probe(client)
        body.update({
            "status": "ok",
            "service": "round_robin",
            "version": __version__,
        })
        return JSONResponse(body)

    @app.get("/api/peers")
    async def get_peers() -> JSONResponse:
        # Mirrors `prompt_enhancer.api.rest.peers` byte-for-byte: returns
        # `{"services": {<name>: <url>, ...}}` so cross-product code can
        # treat the two surfaces identically.
        return JSONResponse({"services": get_all_peers()})

    @app.get("/api/activity")
    async def get_activity(limit: int = 50) -> JSONResponse:
        # Cross-umbrella activity feed. Same wire shape as
        # prompt-enhancer + development. Bounds are clamped to
        # [1, 200] silently — no 422 for the Studio panel.
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200
        return JSONResponse({
            "service": "round_robin",
            "events": activity_module.snapshot(limit),
        })

    @app.get("/api/models")
    async def get_models() -> JSONResponse:
        try:
            models = await client.models()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return JSONResponse({"models": models})

    # ── Run control ────────────────────────────────────────────────────────

    _RESUMABLE_STATUSES = {"running", "paused", "awaiting_user"}

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        if orch.run_id is None:
            saved = SafeStorage.load_json(STATE_FILE, None)
            if isinstance(saved, dict) and saved.get("status") in _RESUMABLE_STATUSES:
                return JSONResponse({"resumable": True, "saved": saved})
        return JSONResponse({"resumable": False, "current": orch.public_state()})

    @app.delete("/api/state")
    async def discard_state() -> JSONResponse:
        # Don't blow away a live run's state
        if orch.is_running():
            raise HTTPException(status_code=409, detail="Cannot discard state of a running run.")
        try:
            STATE_FILE.unlink(missing_ok=True)
            STATE_FILE.with_suffix(STATE_FILE.suffix + ".bak").unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return JSONResponse({"ok": True})

    # ── User config (UI defaults) ──────────────────────────────────────────

    @app.get("/api/config")
    async def get_user_config() -> JSONResponse:
        return JSONResponse(user_config.load())

    @app.patch("/api/config")
    async def patch_user_config(updates: dict) -> JSONResponse:
        if not isinstance(updates, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        try:
            return JSONResponse(user_config.save(updates))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/run/start")
    async def start_run(body: StartRunBody) -> JSONResponse:
        cfg = RunConfig(
            theme=body.theme,
            agents=[AgentConfig(name=a.name, model=a.model, persona=a.persona) for a in body.agents],
            loop_limit=body.loop_limit,
            pause_after_each_turn=body.pause_after_each_turn,
            auto_retry=body.auto_retry,
            auto_retry_backoff_s=body.auto_retry_backoff_s,
            charlie=CharlieConfig(enabled=body.charlie.enabled, model=body.charlie.model),
            intel_collab_directive=body.intel_collab_directive,
            intel_anti_rambling=body.intel_anti_rambling,
            intel_anti_yes_man=body.intel_anti_yes_man,
            intel_redundancy_threshold=body.intel_redundancy_threshold,
            intel_brief_threshold_tokens=body.intel_brief_threshold_tokens,
            intel_agreement_threshold=body.intel_agreement_threshold,
        )
        try:
            run_id = await orch.start(cfg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse({"run_id": run_id})

    @app.post("/api/run/stop")
    async def stop_run() -> JSONResponse:
        await orch.stop()
        return JSONResponse({"ok": True})

    @app.post("/api/run/pause")
    async def pause_run() -> JSONResponse:
        await orch.pause()
        return JSONResponse({"ok": True})

    @app.post("/api/run/resume")
    async def resume_run(body: ResumeBody) -> JSONResponse:
        if orch.status not in (STATUS_PAUSED, STATUS_RUNNING):
            raise HTTPException(status_code=409, detail=f"Cannot resume from {orch.status!r}")
        await orch.resume(injection=body.injection)
        return JSONResponse({"ok": True})

    @app.post("/api/run/choose")
    async def choose(body: ChoiceBody) -> JSONResponse:
        if body.action not in {"retry", "skip", "use_other", "stop"}:
            raise HTTPException(status_code=400, detail="Invalid action.")
        await orch.submit_choice(body.action)
        return JSONResponse({"ok": True})

    # ── Presets ────────────────────────────────────────────────────────────

    @app.get("/api/presets")
    async def list_presets() -> JSONResponse:
        return JSONResponse({"presets": presets.list()})

    @app.post("/api/presets")
    async def create_preset(body: PresetBody) -> JSONResponse:
        try:
            preset = presets.create(body.name, body.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse({"preset": preset})

    @app.patch("/api/presets/{preset_id}")
    async def update_preset(preset_id: str, body: PresetUpdateBody) -> JSONResponse:
        try:
            preset = presets.update(preset_id, name=body.name, config=body.config)
        except KeyError:
            raise HTTPException(status_code=404, detail="Preset not found.")
        return JSONResponse({"preset": preset})

    @app.post("/api/presets/{preset_id}/duplicate")
    async def duplicate_preset(preset_id: str) -> JSONResponse:
        try:
            preset = presets.duplicate(preset_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Preset not found.")
        return JSONResponse({"preset": preset})

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str) -> JSONResponse:
        ok = presets.delete(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Preset not found.")
        return JSONResponse({"ok": True})

    @app.get("/api/presets/{preset_id}/export")
    async def export_preset(preset_id: str) -> JSONResponse:
        preset = presets.get(preset_id)
        if not preset:
            raise HTTPException(status_code=404, detail="Preset not found.")
        return JSONResponse(preset)

    @app.post("/api/presets/import")
    async def import_preset(body: dict) -> JSONResponse:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        try:
            preset = presets.import_one(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse({"preset": preset})

    # ── Sessions ───────────────────────────────────────────────────────────

    @app.get("/api/sessions")
    async def list_sessions(q: str = "") -> JSONResponse:
        return JSONResponse({"sessions": sessions.search(q) if q else sessions.list()})

    @app.get("/api/sessions/{run_id}")
    async def get_session(run_id: str) -> JSONResponse:
        data = sessions.get(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return JSONResponse(data)

    @app.delete("/api/sessions/{run_id}")
    async def delete_session(run_id: str) -> JSONResponse:
        ok = sessions.delete(run_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Session not found.")
        return JSONResponse({"ok": True})

    # ── Errors ─────────────────────────────────────────────────────────────

    @app.get("/api/errors")
    async def list_errors(limit: int = 100, category: str | None = None) -> JSONResponse:
        items = monitor.recent(limit=limit, category=category)
        return JSONResponse({
            "errors": [asdict(e) for e in items],
            "stats": monitor.stats(),
        })

    @app.delete("/api/errors")
    async def clear_errors() -> JSONResponse:
        n = monitor.clear()
        return JSONResponse({"cleared": n})

    @app.exception_handler(Exception)
    async def _global_exc(request, exc):  # noqa: ANN001
        monitor.record(
            "unhandled",
            f"{type(exc).__name__}: {exc}",
            path=str(request.url.path),
            method=request.method,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "type": type(exc).__name__},
        )

    # ── Charlie ────────────────────────────────────────────────────────────

    @app.get("/api/charlie/file")
    async def charlie_file(path: str) -> JSONResponse:
        ws = charlie_get_current()
        if ws is None:
            raise HTTPException(status_code=404, detail="No Charlie session active.")
        try:
            return JSONResponse(ws.read(path))
        except SandboxError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/charlie/summarize")
    async def charlie_summarize(body: SummarizeBody) -> JSONResponse:
        transcript = None
        theme = None
        run_id = None
        if body.session_id:
            data = sessions.get(body.session_id)
            if data is None:
                raise HTTPException(status_code=404, detail="Session not found.")
            transcript = data.get("transcript") or []
            theme = (data.get("config") or {}).get("theme") or ""
            run_id = data.get("run_id") or body.session_id
        try:
            path = await orch.regenerate_summary(
                model=body.model,
                transcript=transcript,
                theme=theme,
                run_id=run_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            # Catch-all so unexpected errors surface as a STRING detail rather
            # than tripping the global handler (which returns a non-string body
            # the frontend would render as "[object Object]"). Common cases:
            # LMLinkError wrapping LM Studio 4xx, httpx connect/timeout errors.
            logger.exception("charlie_summarize unexpected failure")
            monitor.record("charlie", f"summarize failure: {exc}",
                           error_class=type(exc).__name__)
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
        if not path:
            # Surface Charlie's specific failure (busy / empty response / LLM error)
            # so the user sees something actionable instead of a generic message.
            specific = orch._charlie_agent.last_error
            detail = specific or "Charlie failed to produce a summary."
            # Busy state is a 409 (try-again) rather than 502 (server problem)
            status = 409 if specific and "still summarizing" in specific else 502
            raise HTTPException(status_code=status, detail=detail)
        return JSONResponse({"ok": True, "path": path})

    # ── Code review (development integration) ─────────────────────────────

    @app.post("/api/review")
    async def review_layer(body: ReviewRequest) -> JSONResponse:
        """Multi-LLM dialogue code review.

        Consumed by ``development.reviewers.RoundRobinReviewer``. Returns a
        verdict in the contract shape:

            {"approved": bool, "issues": [str], "request_regenerate": bool,
             "agents": {"agent_a_verdict": str, "agent_b_verdict": str,
                        "consensus": str}}

        On dialogue failure (LM Studio unreachable, model unloaded, …) we
        return 503 with a string ``error`` so the dev-side reviewer can
        treat it the same as 404 (deferred-mode fallback).
        """
        activity_module.record_review_request(body.layer)
        try:
            verdict = await review_with_dialogue(
                layer=body.layer,
                purpose=body.purpose,
                files=body.files,
                lm_client=client,
                model=body.model,
            )
        except Exception as exc:
            logger.warning("review_with_dialogue failed: %s", exc)
            monitor.record(
                "review",
                f"review failed: {exc}",
                error_class=type(exc).__name__,
                layer=body.layer,
            )
            return JSONResponse(
                status_code=503,
                content={"error": f"review unavailable: {type(exc).__name__}: {exc}"},
            )
        return JSONResponse(verdict)

    # ── Persona handoff (from prompt-enhancer) ─────────────────────────────
    #
    # Ephemeral, in-memory, one-shot. prompt-enhancer's "Send to Round Robin"
    # POSTs personas + theme here; our UI fetches on page load, prefills, then
    # DELETEs so a refresh doesn't re-stamp the same handoff. Persistence
    # across server restarts is intentionally NOT supported.

    handoff_state: dict[str, Any] = {"pending": None}
    handoff_lock = asyncio.Lock()

    @app.post("/api/persona-handoff")
    async def post_persona_handoff(body: PersonaHandoff) -> JSONResponse:
        if not body.theme.strip():
            raise HTTPException(status_code=400, detail="theme is required")
        stored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        payload = {
            "theme": body.theme,
            "alpha_persona": body.alpha_persona,
            "bravo_persona": body.bravo_persona,
            "source": body.source,
            "stored_at": stored_at,
        }
        async with handoff_lock:
            handoff_state["pending"] = payload
        # Stamp the umbrella activity feed so the user sees the
        # incoming handoff next to prompt-enhancer's outgoing one.
        activity_module.record_persona_handoff_received(body.theme, body.source)
        return JSONResponse({"status": "ok", "stored_at": stored_at})

    @app.get("/api/persona-handoff")
    async def get_persona_handoff() -> Response:
        async with handoff_lock:
            payload = handoff_state["pending"]
        if payload is None:
            return Response(status_code=204)
        return JSONResponse(payload)

    @app.delete("/api/persona-handoff")
    async def delete_persona_handoff() -> Response:
        async with handoff_lock:
            handoff_state["pending"] = None
        return Response(status_code=204)

    # ── WebSocket ──────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await hub.connect(websocket)
        try:
            await websocket.send_text(json.dumps({
                "type": "hello",
                "state": orch.public_state(),
            }, default=str))
            while True:
                # Keep the connection open. The frontend uses REST for control
                # and the WS only for server→client events.
                msg = await websocket.receive_text()
                if msg == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)

    return app


app = create_app()
