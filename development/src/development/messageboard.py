"""SQLite-backed event log — the "message board" from the architecture diagram.

Producers (the orchestrator, stages) call :meth:`publish`. Consumers
(the round-robin UI, the prompt-enhancer ``/api/forward-to/development``
forwarder, the local ``/api/runs`` endpoint) call :meth:`subscribe` for
a live stream or :meth:`recent` for one-shot history.

Concurrency model:

* One SQLite file, opened on demand per call (``check_same_thread=False``
  so async tasks running on different event-loop threads can share the
  same DB without each opening their own connection pool).
* Writes are serialized through a ``threading.Lock`` because SQLite
  itself doesn't enforce single-writer ordering inside a process.
* :meth:`subscribe` polls (not LISTEN/NOTIFY — SQLite has none) at a
  short interval, replaying history first, then yielding new rows.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from .types import StageEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind);
CREATE INDEX IF NOT EXISTS events_ts_idx   ON events(ts);
"""


class MessageBoard:
    """Append-only event log with replay + live tail."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: we serialize writes ourselves with
        # _lock; FastAPI may dispatch handlers on different threads
        # depending on the worker model.
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we control transactions
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ── write ──────────────────────────────────────────────────────

    def publish(self, kind: str, payload: dict[str, Any]) -> int:
        """Append an event, return its rowid.

        Payload is JSON-serialized at write time. If serialization
        fails (e.g. caller passed a non-serializable object), we fall
        back to ``json.dumps(..., default=str)`` so the event still
        gets recorded — losing fidelity is better than dropping the
        event silently.
        """
        try:
            blob = json.dumps(payload)
        except (TypeError, ValueError):
            blob = json.dumps(payload, default=str)
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                (ts, kind, blob),
            )
            return int(cur.lastrowid or 0)

    # ── read ───────────────────────────────────────────────────────

    def recent(self, limit: int = 50) -> list[StageEvent]:
        """Return the ``limit`` newest events, newest-first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, kind, payload FROM events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def all_since(self, since_id: int = 0) -> list[StageEvent]:
        """Return every event with ``id > since_id``, in ascending order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, kind, payload FROM events "
                "WHERE id > ? ORDER BY id ASC",
                (since_id,),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    async def subscribe(
        self,
        kinds: Iterable[str] | None = None,
        *,
        poll_interval: float = 0.05,
        from_id: int = 0,
    ) -> AsyncIterator[StageEvent]:
        """Replay history then yield new events as they arrive.

        ``kinds`` filters by event kind; ``None`` yields everything.
        ``from_id`` lets a reconnecting consumer skip events it's
        already seen. The iterator never terminates on its own —
        cancel the awaiting task to stop subscribing.
        """
        wanted: set[str] | None = set(kinds) if kinds is not None else None
        last_id = from_id
        # Replay
        for event in self.all_since(last_id):
            if wanted is None or event.kind in wanted:
                yield event
            last_id = event.id
        # Tail
        while True:
            await asyncio.sleep(poll_interval)
            for event in self.all_since(last_id):
                if wanted is None or event.kind in wanted:
                    yield event
                last_id = event.id

    # ── lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying SQLite connection. Safe to call twice."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def _row_to_event(row: tuple[int, float, str, str]) -> StageEvent:
    """Decode a (id, ts, kind, payload-as-json) tuple into a StageEvent."""
    eid, ts, kind, payload_json = row
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        # Corrupt row — surface the raw text rather than crash the iterator.
        payload = {"_raw": payload_json}
    return StageEvent(id=eid, ts=ts, kind=kind, payload=payload)
