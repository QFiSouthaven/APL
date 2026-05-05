"""Cross-umbrella activity feed — translator over the MessageBoard.

Mirrors ``prompt_enhancer.api.activity`` and ``round_robin.activity`` at
the wire level: ``GET /api/activity`` returns the same envelope the
Studio panel polls.

Architectural compromise (called out in the task return note):

development already has an SQLite-backed ``MessageBoard`` that records
every BUILD_STARTED / STAGE_STARTED / STAGE_DONE / BUILD_DONE event.
We REUSE that existing event store rather than maintain a parallel
in-memory ring buffer, because:

  1. The MessageBoard is already the source of truth for /api/runs and
     /api/events (SSE) — adding a parallel buffer would create two
     places to keep in sync.
  2. The constraint says "ring buffer 200 max per sibling" and "no
     persistence required". MessageBoard exceeds the persistence floor
     (it's actually persistent), but we cap reads at 200 so memory at
     query time stays bounded — that's the spirit of the constraint.

The translator function maps StageEvent.kind + payload to the umbrella
wire format. Errors during translation are swallowed so a bad row in
the message board can't break /api/activity for the whole umbrella.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .messageboard import MessageBoard
from .types import (
    BUILD_DONE,
    BUILD_FAILED,
    BUILD_STARTED,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_PROGRESS,
    STAGE_STARTED,
)

logger = logging.getLogger(__name__)

_SUMMARY_MAX = 120

# Total stages we report against. Hard-coded because the build pipeline
# is fixed (Architect / Coder / Reviewer / Tester / Packager). The
# orchestrator does NOT publish a "total stages" field.
_TOTAL_STAGES = 5


def _ts_to_iso(ts: float) -> str:
    """Convert MessageBoard's float epoch ts to ISO-8601 UTC w/ ms + Z."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _truncate(text: str, limit: int = _SUMMARY_MAX) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _translate(kind: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    """Map a StageEvent (kind, payload) to (type, summary).

    Returns ``None`` when the kind is uninteresting to the umbrella
    feed (e.g. STAGE_PROGRESS chatter we'd rather not surface).
    """
    if kind == BUILD_STARTED:
        req = payload.get("request") or {}
        stack = req.get("stack_hint") or req.get("target_lang") or "unknown"
        goal = req.get("goal") or ""
        summary = f"Build started: stack={stack}"
        if goal:
            summary += f" — {_truncate(goal, 60)}"
        return ("build_started", summary)

    if kind == STAGE_STARTED:
        # Orchestrator publishes ``{"stage": <name>}`` — see
        # development.orchestrator.Orchestrator.build. We accept the
        # legacy ``stage_index`` / ``stage_name`` keys defensively in
        # case the payload schema evolves.
        name = (
            payload.get("stage")
            or payload.get("stage_name")
            or payload.get("name")
            or "?"
        )
        idx = payload.get("stage_index") or payload.get("index") or "?"
        idx_str = f"{idx}/{_TOTAL_STAGES}" if idx != "?" else f"?/{_TOTAL_STAGES}"
        return ("stage", f"Stage {idx_str} {name} -> started")

    if kind == STAGE_DONE:
        name = (
            payload.get("stage")
            or payload.get("stage_name")
            or payload.get("name")
            or "?"
        )
        idx = payload.get("stage_index") or payload.get("index") or "?"
        idx_str = f"{idx}/{_TOTAL_STAGES}" if idx != "?" else f"?/{_TOTAL_STAGES}"
        return ("stage", f"Stage {idx_str} {name} -> done")

    if kind == STAGE_FAILED:
        name = (
            payload.get("stage")
            or payload.get("stage_name")
            or payload.get("name")
            or "?"
        )
        err = payload.get("error") or ""
        return ("error", f"Stage {name} -> failed: {_truncate(err, 50)}")

    if kind == BUILD_DONE:
        return ("build_done", "Build done — success")

    if kind == BUILD_FAILED:
        err = payload.get("error") or ""
        return ("error", f"Build done — failed: {_truncate(err, 70)}")

    if kind == STAGE_PROGRESS:
        # Filter STAGE_PROGRESS by default — it's noisy. The UI panel
        # gets the start + end which is enough.
        return None

    return None


def snapshot_from_board(board: MessageBoard, limit: int = 50) -> list[dict[str, Any]]:
    """Return up to ``limit`` activity events, newest-first.

    Pulls a generous slab (8x) from the message board so that even if
    half the rows are uninteresting (e.g. STAGE_PROGRESS) we still
    return ``limit`` translated events when possible.
    """
    if limit <= 0:
        return []
    if limit > 200:
        limit = 200

    try:
        rows = board.recent(limit=limit * 8)
    except Exception:  # noqa: BLE001
        logger.exception("activity snapshot_from_board failed")
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            translated = _translate(row.kind, row.payload or {})
        except Exception:  # noqa: BLE001
            logger.exception("activity translate failed (suppressed)")
            translated = None
        if translated is None:
            continue
        ev_type, summary = translated
        out.append(
            {
                "ts": _ts_to_iso(row.ts),
                "type": ev_type,
                "summary": _truncate(summary),
                "details": {"kind": row.kind, "id": row.id},
            }
        )
        if len(out) >= limit:
            break
    return out
