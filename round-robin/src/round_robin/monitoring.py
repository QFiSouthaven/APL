"""Error monitoring.

One central place every error in the app flows through:
  - JSONL append to data/errors.log (rotating at MAX_LOG_BYTES)
  - In-memory ring buffer (deque) of the last N events for fast UI access
  - Optional async broadcast callback (server wires this to the WebSocket hub)

Wired in two ways:
  1. server.py wraps the WS emit() so any '*_error' event is auto-captured.
  2. asyncio loop exception handler routes uncaught task crashes here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR

logger = logging.getLogger(__name__)

ERROR_LOG: Path = DATA_DIR / "errors.log"
MAX_RING_SIZE: int = 200
MAX_LOG_BYTES: int = 5 * 1024 * 1024   # 5 MB then rotate to .1
MAX_LOG_ROTATIONS: int = 3


@dataclass
class ErrorEvent:
    id: str
    timestamp: str
    category: str            # 'agent' | 'charlie' | 'unhandled' | 'system' | 'network'
    severity: str            # 'error' | 'warning'
    message: str
    context: dict[str, Any] = field(default_factory=dict)


class ErrorMonitor:
    def __init__(self, log_path: Path = ERROR_LOG, ring_size: int = MAX_RING_SIZE) -> None:
        self.log_path = Path(log_path)
        self._ring: deque[ErrorEvent] = deque(maxlen=ring_size)
        self._counter = 0
        self._lock = threading.Lock()
        self._broadcast: Callable[[ErrorEvent], Awaitable[None]] | None = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def set_broadcast(self, fn: Callable[[ErrorEvent], Awaitable[None]]) -> None:
        self._broadcast = fn

    def record(
        self,
        category: str,
        message: str,
        *,
        severity: str = "error",
        **context: Any,
    ) -> ErrorEvent:
        with self._lock:
            self._counter += 1
            event = ErrorEvent(
                id=f"err-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{self._counter:04d}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                category=category,
                severity=severity,
                message=str(message)[:2000],
                context={k: _safe(v) for k, v in context.items()},
            )
            self._ring.append(event)
            self._append_to_log(event)
        if self._broadcast is not None:
            try:
                asyncio.get_event_loop().create_task(self._broadcast(event))
            except RuntimeError:
                # No running loop — skip broadcast (e.g. test context)
                pass
        return event

    def recent(self, limit: int = 100, category: str | None = None) -> list[ErrorEvent]:
        with self._lock:
            items = list(self._ring)
        if category:
            items = [e for e in items if e.category == category]
        return items[-limit:][::-1]

    def clear(self) -> int:
        with self._lock:
            n = len(self._ring)
            self._ring.clear()
        return n

    def stats(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._ring)
        by_cat: dict[str, int] = {}
        for e in items:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        return {
            "total": len(items),
            "by_category": by_cat,
            "log_path": str(self.log_path),
            "log_bytes": self.log_path.stat().st_size if self.log_path.exists() else 0,
        }

    def _append_to_log(self, event: ErrorEvent) -> None:
        try:
            self._rotate_if_needed()
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event), default=str) + "\n")
        except OSError as exc:
            # Last-resort: stderr. Never raise from the monitor itself.
            logger.warning("Failed to append error log: %s", exc)

    def _rotate_if_needed(self) -> None:
        try:
            if not self.log_path.exists() or self.log_path.stat().st_size < MAX_LOG_BYTES:
                return
            for i in range(MAX_LOG_ROTATIONS - 1, 0, -1):
                src = self.log_path.with_suffix(self.log_path.suffix + f".{i}")
                dst = self.log_path.with_suffix(self.log_path.suffix + f".{i + 1}")
                if src.exists():
                    shutil.move(str(src), str(dst))
            shutil.move(str(self.log_path), str(self.log_path.with_suffix(self.log_path.suffix + ".1")))
        except OSError as exc:
            logger.warning("Log rotation failed: %s", exc)


def _safe(v: Any) -> Any:
    """Coerce values into something JSON-serializable. Truncate long strings."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v[:500] if isinstance(v, str) and len(v) > 500 else v
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in v[:20]]
    if isinstance(v, dict):
        return {str(k)[:80]: _safe(val) for k, val in list(v.items())[:50]}
    return repr(v)[:500]


def install_asyncio_exception_handler(monitor: ErrorMonitor, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Route uncaught asyncio task exceptions through the monitor."""
    loop = loop or asyncio.get_event_loop()

    def _handler(_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
        exc = ctx.get("exception")
        message = ctx.get("message") or "asyncio loop exception"
        monitor.record(
            "unhandled",
            f"{type(exc).__name__ if exc else 'AsyncioError'}: {message}",
            traceback=repr(exc) if exc else None,
            future=str(ctx.get("future")) if ctx.get("future") else None,
        )

    loop.set_exception_handler(_handler)
