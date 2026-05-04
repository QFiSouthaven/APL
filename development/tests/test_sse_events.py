"""Tests for the /api/events Server-Sent Events endpoint.

We can't use Starlette's ``TestClient`` or ``httpx.ASGITransport`` here:
both buffer the entire response body before returning, which deadlocks
on an open-ended SSE stream. Instead we spin up a real uvicorn server
on a free localhost port and hit it with a real ``httpx.AsyncClient``
that supports proper chunked streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

import httpx
import pytest
import uvicorn

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.server import _sse_event, create_app

from tests.conftest import FakeLMClient


# ── helpers ─────────────────────────────────────────────────────────


def _free_port() -> int:
    """Return a free localhost TCP port. Race-y but fine for tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerThread:
    """Run a uvicorn server in a background thread for the lifetime of one test."""

    def __init__(self, app) -> None:
        self.port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            lifespan="on",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        # Disable uvicorn's own signal handlers — we're a thread.
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "_ServerThread":
        self._thread.start()
        # Wait for the socket to accept connections.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with contextlib.closing(urlopen(self.base_url + "/api/health", timeout=0.5)):
                    return self
            except (URLError, ConnectionError, OSError):
                time.sleep(0.05)
        raise RuntimeError("uvicorn test server failed to start")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5.0)


def _parse_sse(blob: str) -> list[dict]:
    """Parse SSE bytes into a list of records. Comment-only frames are kept."""
    records: list[dict] = []
    cur: dict = {}
    for raw in blob.splitlines():
        line = raw.rstrip("\r")
        if line == "":
            if "data" in cur or cur.get("_comments"):
                records.append(cur)
            cur = {}
            continue
        if line.startswith(":"):
            cur.setdefault("_comments", []).append(line)
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            cur["id"] = value
        elif field == "event":
            cur["event"] = value
        elif field == "data":
            cur["data"] = (cur.get("data", "") + value) if "data" in cur else value
        elif field == "retry":
            cur["retry"] = value
    if "data" in cur or cur.get("_comments"):
        records.append(cur)
    return records


async def _read_stream(
    base_url: str, path: str, *, timeout: float = 4.0,
    headers: dict | None = None, until_data: int = 1,
) -> tuple[int, dict, str]:
    """Open SSE stream, read until N data events arrive (or timeout).

    Returns (status, headers, raw_blob).
    """
    blob = ""
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        async with client.stream("GET", path, headers=headers or {}) as resp:
            status = resp.status_code
            hdrs = dict(resp.headers)
            deadline = time.monotonic() + timeout
            async for chunk in resp.aiter_text():
                blob += chunk
                count = sum(1 for r in _parse_sse(blob) if "data" in r)
                if count >= until_data:
                    return status, hdrs, blob
                if time.monotonic() > deadline:
                    return status, hdrs, blob
    return status, hdrs, blob


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def server(fake_lm: FakeLMClient, tmp_board: MessageBoard):
    orch = Orchestrator(fake_lm, tmp_board)
    app = create_app(message_board=tmp_board, orchestrator=orch)
    with _ServerThread(app) as srv:
        yield srv


# ── tests ───────────────────────────────────────────────────────────


def test_sse_event_helper_format():
    out = _sse_event(42, "STAGE_DONE", {"x": 1, "y": "z"})
    text = out.decode("utf-8")
    assert text.startswith("id: 42\n")
    assert "event: STAGE_DONE\n" in text
    assert "data: " in text
    assert text.endswith("\n\n")
    data_line = [ln for ln in text.splitlines() if ln.startswith("data: ")][0]
    assert json.loads(data_line[len("data: "):]) == {"x": 1, "y": "z"}


@pytest.mark.asyncio
async def test_sse_endpoint_returns_event_stream_content_type(server: _ServerThread):
    async with httpx.AsyncClient(base_url=server.base_url, timeout=4.0) as c:
        async with c.stream("GET", "/api/events?keepalive=0.05") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers.get("cache-control", "").lower().startswith("no-cache")
            async for chunk in resp.aiter_text():
                if chunk:
                    break


@pytest.mark.asyncio
async def test_sse_first_frame_advertises_retry(server: _ServerThread):
    async with httpx.AsyncClient(base_url=server.base_url, timeout=4.0) as c:
        async with c.stream("GET", "/api/events?keepalive=0.05") as resp:
            blob = ""
            async for chunk in resp.aiter_text():
                blob += chunk
                if "retry:" in blob:
                    break
    assert "retry: 5000" in blob


@pytest.mark.asyncio
async def test_sse_replays_history_then_tails(
    server: _ServerThread, tmp_board: MessageBoard
):
    tmp_board.publish("HIST_A", {"i": 1})
    tmp_board.publish("HIST_B", {"i": 2})

    async def publish_later():
        await asyncio.sleep(0.25)
        tmp_board.publish("LIVE_C", {"i": 3})

    pub = asyncio.create_task(publish_later())
    try:
        _, _, blob = await _read_stream(
            server.base_url, "/api/events?keepalive=0.05",
            timeout=3.0, until_data=3,
        )
    finally:
        pub.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pub

    data = [r["event"] for r in _parse_sse(blob) if "data" in r]
    assert data[:2] == ["HIST_A", "HIST_B"]
    assert "LIVE_C" in data


@pytest.mark.asyncio
async def test_sse_kinds_filter_excludes_others(
    server: _ServerThread, tmp_board: MessageBoard
):
    tmp_board.publish("KEEP", {"i": 1})
    tmp_board.publish("DROP", {"i": 2})
    tmp_board.publish("KEEP", {"i": 3})

    _, _, blob = await _read_stream(
        server.base_url, "/api/events?kinds=KEEP&keepalive=0.05",
        timeout=2.0, until_data=2,
    )
    kinds = [r["event"] for r in _parse_sse(blob) if "data" in r]
    assert kinds == ["KEEP", "KEEP"]
    assert "DROP" not in kinds


@pytest.mark.asyncio
async def test_sse_from_id_skips_earlier_events(
    server: _ServerThread, tmp_board: MessageBoard
):
    a = tmp_board.publish("X", {"i": 1})
    b = tmp_board.publish("X", {"i": 2})
    c = tmp_board.publish("X", {"i": 3})
    assert a < b < c

    _, _, blob = await _read_stream(
        server.base_url, f"/api/events?from_id={b}&keepalive=0.05",
        timeout=2.0, until_data=1,
    )
    data_recs = [r for r in _parse_sse(blob) if "data" in r]
    assert data_recs
    ids = [int(r["id"]) for r in data_recs]
    assert all(i > b for i in ids)
    assert ids[0] == c


@pytest.mark.asyncio
async def test_sse_emits_keepalive_when_idle(server: _ServerThread):
    blob = ""
    async with httpx.AsyncClient(base_url=server.base_url, timeout=4.0) as c:
        async with c.stream("GET", "/api/events?keepalive=0.1") as resp:
            deadline = time.monotonic() + 2.5
            async for chunk in resp.aiter_text():
                blob += chunk
                if ": keepalive" in blob:
                    break
                if time.monotonic() > deadline:
                    break
    assert ": keepalive" in blob, f"no keepalive comment seen in: {blob!r}"


@pytest.mark.asyncio
async def test_sse_honors_last_event_id_header(
    server: _ServerThread, tmp_board: MessageBoard
):
    a = tmp_board.publish("X", {"i": 1})
    b = tmp_board.publish("X", {"i": 2})
    c = tmp_board.publish("X", {"i": 3})
    assert a < b < c

    _, _, blob = await _read_stream(
        server.base_url, "/api/events?keepalive=0.05",
        timeout=2.0, until_data=1,
        headers={"Last-Event-ID": str(b)},
    )
    data_recs = [r for r in _parse_sse(blob) if "data" in r]
    assert data_recs
    ids = [int(r["id"]) for r in data_recs]
    assert ids[0] == c


@pytest.mark.asyncio
async def test_sse_client_disconnect_stops_stream(
    server: _ServerThread, tmp_board: MessageBoard
):
    """Open a stream, exit early, then verify a fresh subscription works.

    If the first generator's cleanup hung, the second connection would
    block — the assertion on the second subscription is the disconnect
    test.
    """
    tmp_board.publish("HIST", {"i": 1})

    async with httpx.AsyncClient(base_url=server.base_url, timeout=4.0) as c:
        async with c.stream("GET", "/api/events?keepalive=0.05") as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                if chunk:
                    break

    _, _, blob = await _read_stream(
        server.base_url, "/api/events?keepalive=0.05",
        timeout=2.0, until_data=1,
    )
    data_recs = [r for r in _parse_sse(blob) if "data" in r]
    assert data_recs and data_recs[0]["event"] == "HIST"
