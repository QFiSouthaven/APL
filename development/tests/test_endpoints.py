"""HTTP-surface tests: /api/health, /api/peers, /api/build, /api/runs.

The endpoint tests exercise HTTP-layer behaviour (status codes, JSON
shape, query params), not the full Architect-Coder-Reviewer chain. We
override the orchestrator's pipeline to ``[ArchitectStage]`` so a single
canned LLM response in ``fake_lm`` is enough to drive a build to
completion. Full-pipeline integration is covered in
``tests/test_orchestrator.py``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from development import __version__
from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.server import create_app
from development.stages import ArchitectStage

from tests.conftest import FakeLMClient


@pytest.fixture
def client(fake_lm: FakeLMClient, tmp_board: MessageBoard) -> TestClient:
    orch = Orchestrator(
        fake_lm, tmp_board,
        stages=[ArchitectStage(fake_lm)],
    )
    app = create_app(message_board=tmp_board, orchestrator=orch)
    with TestClient(app) as c:
        yield c


def test_health_returns_apl_contract_blob(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    # Same shape as round-robin and prompt-enhancer.
    assert body == {
        "status": "ok",
        "service": "development",
        "version": __version__,
    }


def test_peers_returns_services_dict(client: TestClient):
    resp = client.get("/api/peers")
    assert resp.status_code == 200
    body = resp.json()
    assert "services" in body
    services = body["services"]
    # All three umbrella products must be present.
    assert "prompt_enhancer" in services
    assert "round_robin" in services
    assert "development" in services


def test_build_runs_synchronously_and_returns_result(client: TestClient):
    resp = client.post(
        "/api/build",
        json={"goal": "a small notes app"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request"]["goal"] == "a small notes app"
    assert body["stages_completed"] == ["architect"]
    assert "stack" in body["plan"]
    assert body["errors"] == []


def test_build_rejects_missing_goal(client: TestClient):
    resp = client.post("/api/build", json={})
    assert resp.status_code == 422


def test_runs_returns_terminal_events_only(
    client: TestClient, tmp_board: MessageBoard
):
    # Trigger one full build so we have a BUILD_DONE row.
    resp = client.post("/api/build", json={"goal": "x"})
    assert resp.status_code == 200

    runs = client.get("/api/runs?limit=10").json()["runs"]
    assert len(runs) >= 1
    kinds = {r["kind"] for r in runs}
    # Only terminal kinds bubble up.
    assert kinds <= {"BUILD_DONE", "BUILD_FAILED"}


def test_runs_rejects_bad_limit(client: TestClient):
    assert client.get("/api/runs?limit=0").status_code == 400
    assert client.get("/api/runs?limit=99999").status_code == 400


@pytest.mark.asyncio
async def test_events_endpoint_returns_event_stream(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    """/api/events must respond 200 with text/event-stream content-type.

    The streaming surface is exercised under a real uvicorn server in
    ``test_sse_events.py`` (TestClient and httpx ASGITransport both
    buffer responses, deadlocking on open-ended streams). Here we rely
    on the dedicated test module for the wire-level check; this test
    only confirms the route exists.
    """
    import httpx
    from development.orchestrator import Orchestrator

    from tests.test_sse_events import _ServerThread

    orch = Orchestrator(fake_lm, tmp_board)
    app = create_app(message_board=tmp_board, orchestrator=orch)
    with _ServerThread(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base_url, timeout=4.0) as ac:
            async with ac.stream("GET", "/api/events?keepalive=0.05") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                async for chunk in resp.aiter_text():
                    if chunk:
                        break


def test_root_returns_html_ui(client: TestClient):
    """GET / must return the static HTML shell."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "<form" in body
    assert "<script" in body


def test_root_html_is_well_formed(client: TestClient):
    """Body should parse as HTML and contain the elements integrations rely on."""
    from html.parser import HTMLParser

    class _Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags: list[str] = []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)

    body = client.get("/").text
    p = _Tags()
    p.feed(body)
    for required in ("html", "head", "body", "form", "textarea", "button", "script"):
        assert required in p.tags, f"missing <{required}> in /"
