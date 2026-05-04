"""HTTP-surface tests: /api/health, /api/peers, /api/build, /api/runs."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from development import __version__
from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.server import create_app

from tests.conftest import FakeLMClient


@pytest.fixture
def client(fake_lm: FakeLMClient, tmp_board: MessageBoard) -> TestClient:
    orch = Orchestrator(fake_lm, tmp_board)
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
