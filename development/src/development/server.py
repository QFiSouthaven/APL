"""FastAPI app — REST surface for the development service.

Endpoints (v0.1):

* ``GET  /``            — minimal HTML shell (single-page UI).
* ``GET  /api/health``  — APL-standard health blob.
* ``GET  /api/peers``   — full discovery table.
* ``POST /api/build``   — run the orchestrator on a BuildRequest.
* ``GET  /api/runs``    — recent BUILD_DONE / BUILD_FAILED events.

The app is built lazily by :func:`create_app` so tests can pass a
custom ``MessageBoard`` / ``LLMClient`` and avoid touching the real
SQLite path or LM Studio.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import SETTINGS
from .discovery import get_all_peers
from .llm_client import LLMClient
from .messageboard import MessageBoard
from .orchestrator import Orchestrator
from .types import BUILD_DONE, BUILD_FAILED, BuildRequest

logger = logging.getLogger("development.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ── Pydantic request bodies ─────────────────────────────────────────


class BuildRequestBody(BaseModel):
    """HTTP body for ``POST /api/build``.

    Mirrors :class:`BuildRequest` but uses pydantic so FastAPI handles
    422 validation for us.
    """

    goal: str = Field(min_length=1)
    stack_hint: str | None = None
    target_lang: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    def to_request(self) -> BuildRequest:
        return BuildRequest(
            goal=self.goal,
            stack_hint=self.stack_hint,
            target_lang=self.target_lang,
            constraints=dict(self.constraints),
        )


# ── App factory ─────────────────────────────────────────────────────


def create_app(
    *,
    llm_client: LLMClient | None = None,
    message_board: MessageBoard | None = None,
    orchestrator: Orchestrator | None = None,
) -> FastAPI:
    """Build a FastAPI app with optionally-injected components.

    Production callers (uvicorn / ``app.py``) construct the app with
    no args, picking up :data:`SETTINGS`. Tests inject a fake LLM and
    a tmp_path-backed MessageBoard so they don't hit the real LM Studio
    or pollute the user's data dir.
    """
    SETTINGS.ensure_dirs()

    if message_board is None:
        message_board = MessageBoard(SETTINGS.message_board_path)
    if orchestrator is None:
        if llm_client is None:
            llm_client = LLMClient()
        orchestrator = Orchestrator(llm_client, message_board)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            message_board.close()

    app = FastAPI(title="development", version=__version__, lifespan=lifespan)

    # Static UI — only mount if the directory has any files. Keeps
    # tests from blowing up if the static dir is empty.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<h1>development</h1><p>UI assets missing.</p>",
            status_code=200,
        )

    # ── /api/health ──────────────────────────────────────────────
    @app.get("/api/health")
    async def get_health() -> JSONResponse:
        # Byte-for-byte the same contract as round-robin's /api/health
        # additive blob: {status, service, version}.
        return JSONResponse(
            {
                "status": "ok",
                "service": "development",
                "version": __version__,
            }
        )

    # ── /api/peers ───────────────────────────────────────────────
    @app.get("/api/peers")
    async def get_peers() -> JSONResponse:
        return JSONResponse({"services": get_all_peers()})

    # ── /api/build ───────────────────────────────────────────────
    @app.post("/api/build")
    async def post_build(body: BuildRequestBody) -> JSONResponse:
        try:
            result = await orchestrator.build(body.to_request())
        except Exception as exc:  # noqa: BLE001 — last-ditch surface
            logger.exception("Build crashed unexpectedly")
            raise HTTPException(
                status_code=500, detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        return JSONResponse(result.to_dict())

    # ── /api/runs ────────────────────────────────────────────────
    @app.get("/api/runs")
    async def get_runs(limit: int = 20) -> JSONResponse:
        if limit <= 0 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be in 1..500")
        # Pull a generous slab of recent events and filter to terminal
        # ones (BUILD_DONE/BUILD_FAILED). recent() returns newest-first.
        slab = message_board.recent(limit=limit * 8)
        terminal = [
            e.to_dict()
            for e in slab
            if e.kind in {BUILD_DONE, BUILD_FAILED}
        ][:limit]
        return JSONResponse({"runs": terminal})

    return app


def _build_default_app() -> FastAPI:
    """Lazily build the default app for ``uvicorn development.server:app``.

    Wrapped in a try/except so a misconfigured LM Studio host doesn't
    crash the import (which would also crash the test suite that
    imports ``server`` to call ``create_app`` with fakes).
    """
    try:
        return create_app()
    except Exception:  # noqa: BLE001 — defer real failure to first request
        logger.exception("Default app construction failed; serving error stub")
        stub = FastAPI(title="development (degraded)", version=__version__)

        @stub.get("/api/health")
        async def _stub_health() -> JSONResponse:  # pragma: no cover
            return JSONResponse(
                {"status": "degraded", "service": "development", "version": __version__},
                status_code=503,
            )

        return stub


# Module-level instance for ``uvicorn development.server:app`` to pick up.
app = _build_default_app()
