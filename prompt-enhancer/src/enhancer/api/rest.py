"""REST adapter for the four-product loop.

Mounts ``POST /api/enhance``, ``GET /api/health``, ``GET /api/peers``
onto whichever FastAPI app the NiceGUI Studio runs under. Sibling
products (round-robin, development, swarm-loop) call these without
importing this package.

The shared envelope is documented at ``docs/INTEGRATION.md``. Schema
version negotiation is the consumer's responsibility — this module
ALWAYS emits the latest version.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from dataclasses import fields

from . import ENVELOPE_SCHEMA_VERSION
from .discovery import get_all_peers, get_peer_url
from .. import __version__
from ..config import Settings, db_path, jsonl_log_path, load, save_settings
from ..core.events import EventType
from ..core.pipeline import PipelineOptions, build_resume_state, run_pipeline
from ..llm.registry import get_provider
from ..persistence import runs as runs_module
from ..persistence import sessions as sessions_module


# ── request / response models ────────────────────────────────────────

class EnhanceRequest(BaseModel):
    """Inbound request for ``POST /api/enhance``."""

    prompt: str = Field(..., min_length=1)
    model: str | None = None
    scorer_model: str | None = None
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens_scale: float = Field(1.0, ge=0.3, le=3.0)
    persona_mode: bool = False
    complement_persona: bool = False
    magnitude_mode: bool = False
    sot_mode: bool = False
    skip_clarify: bool = True  # sibling products default to non-interactive
    session_id: str | None = None
    loop_iteration: int = 0   # provenance hint from the loop driver


class ProvenanceModel(BaseModel):
    source: str = "prompt_enhancer"
    run_id: str
    ts: str
    loop_iteration: int = 0


class EnhancedEnvelope(BaseModel):
    """Outbound envelope — the cross-product contract."""

    schema_version: str = ENVELOPE_SCHEMA_VERSION
    prompt: str
    enhanced_prompt: str
    task_type: str = ""
    technique: str = ""
    persona: str | None = None
    persona_partner: str | None = None
    scores: dict[str, int] = Field(default_factory=dict)
    scores_fallback: bool = False
    pass3_partial: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: ProvenanceModel
    extras: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    ok: bool = True
    version: str = __version__
    default_model: str = ""
    schema_version: str = ENVELOPE_SCHEMA_VERSION


# ── router ──────────────────────────────────────────────────────────

router = APIRouter(prefix="/api", tags=["integration"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Quick health probe for sibling products."""
    return HealthResponse(default_model=load().default_model)


@router.get("/peers")
async def peers() -> dict[str, dict[str, str]]:
    """Return the configured peer service URLs."""
    return {"services": get_all_peers()}


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    task_type: str | None = None,
    min_improvement: int | None = None,
) -> list[dict[str, Any]]:
    """Return recent runs from SQLite for peer products / dashboards.

    The shape mirrors :func:`enhancer.persistence.runs.list_recent` —
    it's already cross-product-friendly. We pass the params through
    unchanged for v1.2.
    """
    return runs_module.list_recent(
        db_path(),
        limit=limit,
        task_type=task_type,
        min_improvement=min_improvement,
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """Return a single run by ID. 404 if not found."""
    run = runs_module.get_run(db_path(), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/sessions")
async def list_sessions(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    """Return recent sessions, newest-first.

    ``sessions.list_all`` is already updated_at DESC; we slice the
    result to honor ``limit``.
    """
    summaries = sessions_module.list_all(db_path())
    return [
        {
            "id": s.id,
            "name": s.name,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "entry_count": s.entry_count,
            "is_active": s.is_active,
        }
        for s in summaries[:limit]
    ]


@router.post("/forward-to/{peer}")
async def forward_to_peer(peer: str, req: EnhanceRequest) -> Response:
    """Forward an enhance request to a peer service's ``/api/enhance``.

    Returns the peer's response body **verbatim** (raw bytes) so peer
    schema_version drift doesn't break this endpoint. The caller is
    responsible for parsing the body.
    """
    import json as _json

    peer_url = get_peer_url(peer)
    if not peer_url:
        return Response(
            content=_json.dumps({"error": "unknown peer"}),
            status_code=404,
            media_type="application/json",
        )

    target = f"{peer_url}/api/enhance"
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(target, json=req.model_dump())
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
        return Response(
            content=_json.dumps({"error": f"peer unreachable: {exc}"}),
            status_code=502,
            media_type="application/json",
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@router.post("/settings")
async def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist settings to the TOML file at ``config.settings_path()``.

    Accepts a JSON object whose keys match ``Settings`` fields. Unknown
    keys → 400. Values that cannot be coerced to the field's type → 400.
    The current settings (read via ``config.load()``) are merged with
    the incoming payload so callers can patch a subset of fields.
    """
    valid_names = {f.name: type(getattr(Settings(), f.name)) for f in fields(Settings)}

    # Reject unknown keys.
    unknown = set(payload.keys()) - set(valid_names.keys())
    if unknown:
        raise HTTPException(400, detail=f"Unknown setting keys: {sorted(unknown)}")

    # Type-validate each provided key against its dataclass field type.
    coerced: dict[str, Any] = {}
    for name, raw in payload.items():
        expected = valid_names[name]
        try:
            if expected is bool:
                if not isinstance(raw, bool):
                    raise TypeError(f"{name} must be bool")
                coerced[name] = raw
            elif expected is int:
                # Reject bool-as-int (Python: True == 1) for clarity.
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise TypeError(f"{name} must be int")
                coerced[name] = raw
            elif expected is float:
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise TypeError(f"{name} must be number")
                coerced[name] = float(raw)
            elif expected is str:
                if not isinstance(raw, str):
                    raise TypeError(f"{name} must be string")
                coerced[name] = raw
            else:  # pragma: no cover — Settings only uses these four types
                raise TypeError(f"unsupported type for {name}")
        except TypeError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    # Merge into current settings — partial updates are supported.
    current = load()
    merged_kwargs = {f.name: getattr(current, f.name) for f in fields(Settings)}
    merged_kwargs.update(coerced)
    new_settings = Settings(**merged_kwargs)

    written = save_settings(new_settings)
    return {"ok": True, "path": str(written)}


@router.post("/enhance", response_model=EnhancedEnvelope)
async def enhance(req: EnhanceRequest) -> EnhancedEnvelope:
    """Run the 4-pass enhancer and return the envelope.

    Auto-resumes on disambiguation pause (mirrors `_run_with_auto_resume`
    in the CLI's compare/batch path); sibling products always get a
    completed envelope, never an empty sentinel.
    """
    settings = load()
    provider = get_provider(settings)
    chosen_model = req.model or settings.default_model
    if not chosen_model:
        models = await provider.list_models()
        chosen_model = models[0] if models else ""
    if not chosen_model:
        raise HTTPException(503, detail="No model available on the configured provider")

    pending: dict[str, dict] = {}
    captured: dict[str, Any] = {}

    async def _on_event(event_type, **payload):
        name = event_type.value if hasattr(event_type, "value") else str(event_type)
        if name == EventType.AGENT_DISAMBIGUATE.value:
            captured["disambig_id"] = payload.get("disambig_id")
            captured["questions"] = payload.get("questions") or []

    opts = PipelineOptions(
        scorer_model=req.scorer_model,
        magnitude_mode=req.magnitude_mode,
        persona_mode=req.persona_mode,
        complement_persona=req.complement_persona,
        sot_mode=req.sot_mode,
        temperature=req.temperature,
        max_tokens_scale=req.max_tokens_scale,
        session_id=req.session_id,
    )

    result = await run_pipeline(
        req.prompt,
        provider=provider, model=chosen_model,
        opts=opts,
        on_event=_on_event,
        request_timeout=settings.request_timeout,
        idle_timeout=settings.idle_timeout,
        pending_disambig=pending,
    )

    # Auto-resume the disambiguation pause for sibling-product calls.
    if (
        req.skip_clarify
        and result.extras
        and result.extras.get("paused")
        and captured.get("disambig_id")
    ):
        snapshot = pending.get(captured["disambig_id"])
        if snapshot is not None:
            resume_state = build_resume_state(snapshot, {})
            result = await run_pipeline(
                snapshot["prompt"],
                provider=provider, model=chosen_model,
                opts=PipelineOptions(
                    scorer_model=snapshot.get("scorer_model"),
                    persona_mode=snapshot.get("persona_mode", False),
                    complement_persona=snapshot.get("complement_persona", False),
                    magnitude_mode=snapshot.get("magnitude_mode", False),
                    sot_mode=snapshot.get("sot_mode", False),
                    session_id=snapshot.get("session_id"),
                    temperature=req.temperature,
                    max_tokens_scale=req.max_tokens_scale,
                    resume_state=resume_state,
                ),
                on_event=_on_event,
                request_timeout=settings.request_timeout,
                idle_timeout=settings.idle_timeout,
            )

    # Persist (dual-write JSONL for one release).
    record = result.extras.get("_record") if result.extras else None
    if record is not None:
        runs_module.save(record, db_path(), jsonl_log_path())

    return EnhancedEnvelope(
        prompt=req.prompt,
        enhanced_prompt=result.result,
        task_type=result.task_type or "",
        technique=result.technique or "",
        persona=result.persona,
        persona_partner=result.persona_partner,
        scores=result.scores or {},
        scores_fallback=result.scores_fallback,
        pass3_partial=result.pass3_partial,
        metadata={
            "model": result.model,
            "scorer_model": result.scorer_model,
            "temperature": req.temperature,
            "max_tokens_scale": req.max_tokens_scale,
            "pass_times_ms": result.pass_times_ms,
            "magnitude_output": result.magnitude_output,
            "sot_output": result.sot_output,
        },
        provenance=ProvenanceModel(
            run_id=result.run_id or "",
            ts=datetime.now(timezone.utc).isoformat(),
            loop_iteration=req.loop_iteration,
        ),
    )
