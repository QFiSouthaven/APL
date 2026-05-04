"""Tests for the SQLite-backed MessageBoard."""

from __future__ import annotations

import asyncio
import threading

import pytest

from development.messageboard import MessageBoard
from development.types import StageEvent


def test_publish_then_recent_returns_in_newest_first(tmp_board: MessageBoard):
    a = tmp_board.publish("KIND_A", {"x": 1})
    b = tmp_board.publish("KIND_B", {"x": 2})
    c = tmp_board.publish("KIND_C", {"x": 3})

    assert a < b < c
    rows = tmp_board.recent(limit=10)
    assert [r.kind for r in rows] == ["KIND_C", "KIND_B", "KIND_A"]
    # Each entry is a StageEvent with proper payload decoding
    assert all(isinstance(r, StageEvent) for r in rows)
    assert rows[0].payload == {"x": 3}


def test_recent_respects_limit(tmp_board: MessageBoard):
    for i in range(5):
        tmp_board.publish("X", {"i": i})
    rows = tmp_board.recent(limit=2)
    assert len(rows) == 2


def test_all_since_returns_only_after_id(tmp_board: MessageBoard):
    a = tmp_board.publish("X", {"i": 1})
    b = tmp_board.publish("X", {"i": 2})
    c = tmp_board.publish("X", {"i": 3})

    rows = tmp_board.all_since(a)
    assert [r.id for r in rows] == [b, c]


def test_publish_handles_non_serializable_payload(tmp_board: MessageBoard):
    class Custom:
        def __str__(self) -> str:
            return "custom-obj"

    # Dict with a non-JSON-able value — must not raise; falls back to default=str.
    eid = tmp_board.publish("WEIRD", {"obj": Custom()})
    assert eid > 0
    rows = tmp_board.recent(limit=1)
    assert rows[0].kind == "WEIRD"
    assert "custom-obj" in str(rows[0].payload)


@pytest.mark.asyncio
async def test_subscribe_replays_history_then_streams_new(tmp_board: MessageBoard):
    # Pre-existing events
    tmp_board.publish("HIST", {"i": 1})
    tmp_board.publish("HIST", {"i": 2})

    seen: list[StageEvent] = []
    stop = asyncio.Event()

    async def consume() -> None:
        async for ev in tmp_board.subscribe(poll_interval=0.01):
            seen.append(ev)
            if len(seen) == 4:
                stop.set()
                break

    task = asyncio.create_task(consume())
    # Yield so the consumer can replay the two historical events first.
    await asyncio.sleep(0.05)
    tmp_board.publish("LIVE", {"i": 3})
    tmp_board.publish("LIVE", {"i": 4})

    await asyncio.wait_for(stop.wait(), timeout=2.0)
    task.cancel()
    with pytest.suppress(asyncio.CancelledError) if hasattr(pytest, "suppress") else _noctx():
        try:
            await task
        except asyncio.CancelledError:
            pass

    kinds = [e.kind for e in seen]
    # Replay yields HIST then HIST; tail yields LIVE then LIVE.
    assert kinds == ["HIST", "HIST", "LIVE", "LIVE"]


@pytest.mark.asyncio
async def test_subscribe_filters_by_kind(tmp_board: MessageBoard):
    tmp_board.publish("KEEP", {"i": 1})
    tmp_board.publish("DROP", {"i": 2})
    tmp_board.publish("KEEP", {"i": 3})

    seen: list[StageEvent] = []

    async def consume() -> None:
        async for ev in tmp_board.subscribe(kinds=["KEEP"], poll_interval=0.01):
            seen.append(ev)
            if len(seen) == 2:
                break

    await asyncio.wait_for(consume(), timeout=2.0)
    assert [e.payload["i"] for e in seen] == [1, 3]


def test_publish_is_thread_safe(tmp_path):
    """Hammer publish from multiple threads; row count must match."""
    board = MessageBoard(tmp_path / "mb.sqlite3")
    try:
        n_threads = 8
        per_thread = 50

        def worker(tid: int) -> None:
            for i in range(per_thread):
                board.publish("HAMMER", {"tid": tid, "i": i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No corruption; full count present.
        rows = board.recent(limit=n_threads * per_thread + 10)
        assert len(rows) == n_threads * per_thread
    finally:
        board.close()


def test_close_is_idempotent(tmp_path):
    board = MessageBoard(tmp_path / "mb.sqlite3")
    board.close()
    board.close()  # must not raise


# Tiny shim for pytest versions without `pytest.suppress`.
class _noctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False
