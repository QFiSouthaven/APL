"""Cross-umbrella activity feed — in-memory ring buffer.

Every sibling (prompt-enhancer, round-robin, development) exposes the
SAME ``GET /api/activity`` endpoint: a small JSON envelope of recent
lifecycle events the user wants to *see* across the umbrella without
reading source.

Wire format (frozen — must match across all three siblings):

    {
      "service": "<sibling-name>",
      "events": [
        {
          "ts": "2026-05-04T15:30:14.123Z",
          "type": "<event-type-string>",
          "summary": "<plain-text one-liner, ≤120 chars>",
          "details": {<optional structured payload>}
        },
        ...
      ]
    }

Rules: events newest-first, ts is ISO-8601 UTC w/ ms + trailing Z,
``summary`` ≤120 chars (truncate with "..."), default limit 50, max 200.
Persistence is intentionally NOT supported — activity is ephemeral.

The buffer is a module-level ``collections.deque`` with bounded maxlen
so memory stays flat regardless of run count. ``record()`` is best-effort:
exceptions are swallowed so a recording bug never breaks the request
that triggered the event.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Deque

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)


# ── ring buffer ──────────────────────────────────────────────────────

# Max events kept in memory per sibling. The constraint says "Ring
# buffer size 200 max per sibling" — keep memory bounded.
_MAX_BUFFER = 200
_SUMMARY_MAX = 120

_BUFFER: Deque[dict[str, Any]] = deque(maxlen=_MAX_BUFFER)
_LOCK = Lock()


def _now_iso() -> str:
    """ISO-8601 UTC with millisecond precision and trailing Z."""
    now = datetime.now(timezone.utc)
    # Python's isoformat() gives microseconds and either +00:00 or no
    # offset; we want exactly ms + Z so the wire format is identical
    # across siblings regardless of how each emits time.
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _truncate(text: str, limit: int = _SUMMARY_MAX) -> str:
    """Trim ``text`` to ``limit`` chars with a "..." suffix when it overflows."""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def record(event_type: str, summary: str, details: dict[str, Any] | None = None) -> None:
    """Append an event to the ring buffer (best-effort, never raises)."""
    try:
        item: dict[str, Any] = {
            "ts": _now_iso(),
            "type": str(event_type or "unknown"),
            "summary": _truncate(summary or ""),
        }
        if details:
            # Filter out unserializable values defensively. We don't
            # JSON-encode here — FastAPI does that at response time —
            # but we coerce anything weird to a string.
            try:
                import json
                json.dumps(details)
                item["details"] = details
            except (TypeError, ValueError):
                item["details"] = {k: str(v) for k, v in details.items()}
        with _LOCK:
            _BUFFER.append(item)
    except Exception:  # noqa: BLE001
        # Activity recording must NEVER break the caller. Log and move on.
        logger.exception("activity.record failed (suppressed)")


def snapshot(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to ``limit`` events, newest-first.

    The caller already validates the bounds; we still clamp here so a
    rogue caller can't drain memory.
    """
    if limit <= 0:
        return []
    if limit > _MAX_BUFFER:
        limit = _MAX_BUFFER
    with _LOCK:
        items = list(_BUFFER)
    items.reverse()  # newest-first
    return items[:limit]


def clear() -> None:
    """Drop all buffered events. Used by tests."""
    with _LOCK:
        _BUFFER.clear()


# ── FastAPI router ───────────────────────────────────────────────────

# The router is shared across siblings via a small factory so each
# sibling can stamp its own ``service`` name. prompt-enhancer's
# rest.py mounts this on the main router; the other two siblings
# implement their own equivalent — same wire shape.

def make_activity_router(service_name: str) -> APIRouter:
    """Build a router with ``GET /api/activity`` mounted under it.

    ``service_name`` is the sibling identifier embedded in the
    response envelope so the Studio panel can color-code rows by
    origin without an extra header.
    """
    router = APIRouter(prefix="/api", tags=["activity"])

    @router.get("/activity")
    async def get_activity(
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"service": service_name, "events": snapshot(limit)}

    return router


# ── pipeline-event adapter (prompt-enhancer-specific) ────────────────

# The pipeline emits ``EventType`` enum members + payloads via the
# ``on_event`` callback. We translate the subset that's interesting to
# a human (run start, per-pass completion, persona, scoring, errors)
# into one-line summaries.
#
# Imported lazily inside the function so a downstream sibling that
# wants to use the activity module without pulling in the whole
# enhancer.core surface still can.

def make_pipeline_recorder(prompt: str):
    """Return an ``on_event`` wrapper that records pipeline lifecycle events.

    Caller threads the returned callable into ``run_pipeline(on_event=...)``
    instead of the bare callback. The wrapper:

      * records run-start / pass-result / persona / score events
      * delegates to the original ``on_event`` for everything (so tests
        that count events still see the full stream).

    Use ``recorder.set_inner(cb)`` to plug in the original callback —
    the rest.py handler does that to capture disambig state too.
    """
    # Local import to keep the module loadable from contexts that
    # don't have the full enhancer.core dependency tree on sys.path
    # (e.g. round-robin running this module — though it has its own).
    from ..core.events import EventType

    state: dict[str, Any] = {"inner": None, "prompt": prompt or ""}

    def set_inner(cb):
        state["inner"] = cb

    async def on_event(event_type, **payload):
        # Translate to activity entry first — never let a recording
        # bug derail the inner callback chain.
        try:
            name = event_type.value if hasattr(event_type, "value") else str(event_type)
            _record_pipeline_event(name, state["prompt"], payload)
        except Exception:  # noqa: BLE001
            logger.exception("activity pipeline recorder failed (suppressed)")
        # Delegate to the inner callback (e.g. UI streaming, test collector).
        inner = state["inner"]
        if inner is not None:
            await inner(event_type, **payload)

    on_event.set_inner = set_inner  # type: ignore[attr-defined]
    return on_event


def _record_pipeline_event(name: str, prompt: str, payload: dict[str, Any]) -> None:
    """Translate a pipeline EventType + payload to an activity entry."""
    # Run start — no dedicated event in the enum, so we stamp on the
    # FIRST AGENT_PASS_START (pass_number=1). Cheap and correct.
    if name == "agent_pass_start" and payload.get("pass_number") == 1:
        record(
            "run_started",
            f"Run started: {_truncate(prompt, 80)}",
            {"prompt_len": len(prompt or "")},
        )
        return

    if name == "agent_pass_result":
        pn = payload.get("pass_number")
        pname = payload.get("pass_name") or ""
        ms = payload.get("duration_ms") or 0
        record(
            "pass_result",
            f"Pass {pn} {pname} done in {ms}ms",
            {"pass_number": pn, "duration_ms": ms},
        )
        return

    if name == "persona_result":
        persona = payload.get("persona") or payload.get("text") or ""
        record(
            "persona_result",
            f"Persona A: {_truncate(persona, 60)}",
            {"persona_len": len(persona)},
        )
        return

    if name == "persona_partner_result":
        partner = payload.get("persona_partner") or payload.get("text") or ""
        record(
            "persona_partner_result",
            f"Persona B: {_truncate(partner, 60)}",
            {"partner_len": len(partner)},
        )
        return

    if name == "enhancement_score":
        scores = payload.get("scores") or {}
        improvement = scores.get("improvement", "?")
        record(
            "run_done",
            f"Run done — improvement {improvement}%",
            {"scores": scores},
        )
        return

    if name == "agent_error":
        msg = payload.get("error") or payload.get("message") or "unknown error"
        record("error", _truncate(str(msg), _SUMMARY_MAX))
        return


def record_persona_handoff(peer: str, theme: str, alpha: str, bravo: str) -> None:
    """One-shot helper for the "Send to Round Robin" persona handoff button.

    Called from rest.py / the UI when the user fires a handoff. The
    summary captures *what was sent* so the user can correlate the
    outgoing event with the incoming event on the round-robin side.
    """
    record(
        "persona_handoff",
        (
            f"Persona handoff -> {peer} "
            f"(theme={len(theme or '')}b, alpha={len(alpha or '')}c, "
            f"bravo={len(bravo or '')}c)"
        ),
        {
            "peer": peer,
            "theme_bytes": len(theme or ""),
            "alpha_chars": len(alpha or ""),
            "bravo_chars": len(bravo or ""),
        },
    )
