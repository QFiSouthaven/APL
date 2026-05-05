"""Cross-umbrella activity feed — in-memory ring buffer.

Mirrors ``prompt_enhancer.api.activity`` byte-for-byte at the wire
level: the Studio panel polls every sibling's ``GET /api/activity``
and treats responses identically.

Wire envelope (frozen across all three siblings):

    {
      "service": "round_robin",
      "events": [
        {"ts": "...Z", "type": "...", "summary": "...", "details": {...}},
        ...
      ]
    }

Round-robin piggybacks on the existing ``emit(event, **fields)`` callable
the orchestrator already uses for WebSocket broadcasts. ``server.py``
wraps that callable so every event the orchestrator publishes also
flows into this buffer — no parallel pub/sub channel.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Deque

logger = logging.getLogger(__name__)

_MAX_BUFFER = 200
_SUMMARY_MAX = 120

_BUFFER: Deque[dict[str, Any]] = deque(maxlen=_MAX_BUFFER)
_LOCK = Lock()


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _truncate(text: str, limit: int = _SUMMARY_MAX) -> str:
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
            try:
                import json
                json.dumps(details)
                item["details"] = details
            except (TypeError, ValueError):
                item["details"] = {k: str(v) for k, v in details.items()}
        with _LOCK:
            _BUFFER.append(item)
    except Exception:  # noqa: BLE001
        logger.exception("activity.record failed (suppressed)")


def snapshot(limit: int = 50) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    if limit > _MAX_BUFFER:
        limit = _MAX_BUFFER
    with _LOCK:
        items = list(_BUFFER)
    items.reverse()
    return items[:limit]


def clear() -> None:
    """Drop all buffered events. Used by tests."""
    with _LOCK:
        _BUFFER.clear()


# ── orchestrator-event adapter ──────────────────────────────────────

def record_emit(event: str, fields: dict[str, Any]) -> None:
    """Translate an orchestrator emit() event to an activity entry.

    Called from a thin wrapper around the orchestrator's ``emit``
    callable in ``server.create_app``. We map only the events that
    matter to a human watching the umbrella feed; everything else is
    dropped silently.
    """
    try:
        if event == "run_started":
            cfg = fields.get("config") or {}
            agents = cfg.get("agents") or []
            theme = cfg.get("theme") or ""
            alpha = (agents[0].get("name") if len(agents) >= 1 else "?") or "?"
            bravo = (agents[1].get("name") if len(agents) >= 2 else "?") or "?"
            record(
                "run_started",
                f"Dialogue started: Alpha={alpha}, Bravo={bravo}, theme={_truncate(theme, 40)}",
                {"run_id": fields.get("run_id"), "agent_count": len(agents)},
            )
            return

        if event == "turn_done":
            turn = fields.get("turn")
            agent = fields.get("agent_name") or "?"
            content = fields.get("content") or ""
            record(
                "turn_done",
                f"Turn {turn}: {agent} spoke ({len(content)} chars)",
                {"turn": turn, "agent": agent, "chars": len(content)},
            )
            return

        if event == "run_done":
            turns = fields.get("turns_completed", "?")
            status = fields.get("status") or ""
            record(
                "run_done",
                f"Dialogue done — {turns} turns ({status})",
                {"status": status, "turns_completed": turns,
                 "run_id": fields.get("run_id")},
            )
            return

        if event == "agent_error":
            msg = fields.get("message") or fields.get("error") or "agent error"
            record("error", _truncate(str(msg)))
            return
    except Exception:  # noqa: BLE001
        logger.exception("activity.record_emit failed (suppressed)")


def record_persona_handoff_received(theme: str, source: str) -> None:
    """Stamp activity when prompt-enhancer POSTs to /api/persona-handoff."""
    record(
        "persona_handoff",
        f"Personas received from {source} (theme={len(theme or '')}b)",
        {"source": source, "theme_bytes": len(theme or "")},
    )


def record_review_request(layer: str) -> None:
    """Stamp activity when development POSTs to /api/review."""
    record(
        "review_request",
        f"Review requested: {_truncate(layer, 60)}",
        {"layer": layer},
    )
