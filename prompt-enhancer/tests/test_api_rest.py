"""Tests for ``enhancer.api.rest`` — POST /api/enhance + health.

Uses FastAPI's TestClient against a router that's been wired to a
fake provider via monkeypatch. Asserts the returned envelope matches
the documented schema and includes real Pass 4 scores (not P4_DEFAULTS).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enhancer.api import rest as rest_module
from enhancer.api.rest import router
from enhancer.persistence import runs as runs_persist
from enhancer.persistence import sessions as sessions_persist
from enhancer.persistence.runs import RunRecord


# Canned LLM responses sufficient for a clean run (no disambiguation).
PASS1_TOKENS = [
    "GOAL: build a thing\n",
    "DOMAIN: software\n",
    "TASK TYPE: coding\n",
    "AUDIENCE: devs\n",
    "IMPLICIT NEEDS: clarity\n",
]
PASS2_TOKENS = [
    "VAGUE TERMS: none\n",
    "MISSING CONTEXT: none\n",
    "UNSTATED CONSTRAINTS: none\n",
    "SCOPE ISSUES: none\n",
    "PRIMARY FOCUS: precision\n",
]
PASS3_TOKENS = ["Enhanced ", "via ", "REST."]
PASS4_TOKENS = ["SPECIFICITY: 9\nCONSTRAINTS: 9\nACTIONABILITY: 9\nIMPROVEMENT: 80\n"]


@pytest.fixture
def fake_app(fake_provider, monkeypatch):
    """Build a FastAPI app with the integration router and a fake provider."""
    fake_provider.stream_responses.extend([
        PASS1_TOKENS, PASS2_TOKENS, PASS3_TOKENS, PASS4_TOKENS,
    ])
    fake_provider.available_models = ["fake-7b"]

    monkeypatch.setattr(rest_module, "get_provider", lambda settings: fake_provider)
    # Settings.default_model unset → router falls through to list_models()[0]

    app = FastAPI()
    app.include_router(router)
    return app


def test_health_endpoint(fake_app):
    client = TestClient(fake_app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["schema_version"] == rest_module.ENVELOPE_SCHEMA_VERSION
    assert "version" in body


def test_peers_endpoint(fake_app):
    client = TestClient(fake_app)
    r = client.get("/api/peers")
    assert r.status_code == 200
    body = r.json()
    assert "services" in body
    assert "prompt_enhancer" in body["services"]
    assert "round_robin" in body["services"]
    assert "development" in body["services"]


def test_enhance_returns_envelope_with_real_scores(fake_app, tmp_path, monkeypatch):
    # Redirect persistence to tmp so tests don't touch user data dir.
    monkeypatch.setattr(rest_module, "db_path", lambda: tmp_path / "enhancer.db")
    monkeypatch.setattr(rest_module, "jsonl_log_path", lambda: tmp_path / "log.jsonl")

    client = TestClient(fake_app)
    r = client.post("/api/enhance", json={
        "prompt": "Make me a thing.",
        "temperature": 0.7,
        "max_tokens_scale": 1.0,
    })

    assert r.status_code == 200, r.text
    env = r.json()

    # Schema fields present
    assert env["schema_version"] == rest_module.ENVELOPE_SCHEMA_VERSION
    assert env["prompt"] == "Make me a thing."
    assert env["enhanced_prompt"] == "Enhanced via REST."
    assert env["task_type"] == "coding"  # via the instructional + code-keyword override
    assert env["technique"] == "precision"
    assert env["scores_fallback"] is False
    assert env["pass3_partial"] is False

    # Real scores, not P4_DEFAULTS
    assert env["scores"]["improvement"] == 80
    assert env["scores"]["specificity"] == 9

    # Provenance carries through
    assert env["provenance"]["source"] == "prompt_enhancer"
    assert env["provenance"]["run_id"]
    assert env["provenance"]["loop_iteration"] == 0

    # Metadata carries the model + timing
    assert "pass_times_ms" in env["metadata"]
    assert env["metadata"]["model"] == "fake-7b"


def test_enhance_rejects_empty_prompt(fake_app):
    client = TestClient(fake_app)
    r = client.post("/api/enhance", json={"prompt": ""})
    assert r.status_code == 422  # pydantic validation


def test_enhance_loop_iteration_propagates(fake_app, tmp_path, monkeypatch):
    monkeypatch.setattr(rest_module, "db_path", lambda: tmp_path / "enhancer.db")
    monkeypatch.setattr(rest_module, "jsonl_log_path", lambda: tmp_path / "log.jsonl")

    client = TestClient(fake_app)
    r = client.post("/api/enhance", json={
        "prompt": "Make me a thing.",
        "loop_iteration": 3,
    })
    assert r.status_code == 200
    assert r.json()["provenance"]["loop_iteration"] == 3


# ── /api/runs and /api/sessions ─────────────────────────────────────


@pytest.fixture
def populated_db(tmp_path, monkeypatch):
    """Seed a fresh SQLite DB with two sessions and two runs, then
    point ``rest_module.db_path`` at it."""
    db = tmp_path / "enhancer.db"
    monkeypatch.setattr(rest_module, "db_path", lambda: db)

    s1 = sessions_persist.create(db, name="alpha")
    s2 = sessions_persist.create(db, name="beta")

    r1 = RunRecord(
        prompt="first prompt", enhanced_prompt="first enhanced",
        task_type="coding", technique="precision",
        scores={"specificity": 8, "constraints": 8, "actionability": 8, "improvement": 70},
        session_id=s1.id, model="fake-7b",
    )
    r2 = RunRecord(
        prompt="second prompt", enhanced_prompt="second enhanced",
        task_type="writing", technique="quality",
        scores={"specificity": 9, "constraints": 9, "actionability": 9, "improvement": 90},
        session_id=s2.id, model="fake-7b",
    )
    runs_persist.save(r1, db, jsonl_path=None)
    runs_persist.save(r2, db, jsonl_path=None)

    app = FastAPI()
    app.include_router(router)
    return {"app": app, "db": db, "run_ids": [r1.id, r2.id],
            "session_ids": [s1.id, s2.id]}


def test_runs_endpoint_returns_list(populated_db):
    client = TestClient(populated_db["app"])
    r = client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    ids = {row["id"] for row in body}
    assert ids == set(populated_db["run_ids"])
    # Smoke-check enriched score fields.
    by_task = {row["task_type"]: row for row in body}
    assert by_task["writing"]["improvement"] == 90


def test_runs_endpoint_respects_limit_and_filters(populated_db):
    client = TestClient(populated_db["app"])
    r = client.get("/api/runs", params={"limit": 1})
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = client.get("/api/runs", params={"task_type": "coding"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["task_type"] == "coding"

    r = client.get("/api/runs", params={"min_improvement": 80})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["task_type"] == "writing"


def test_run_detail_endpoint_404_when_missing(populated_db):
    client = TestClient(populated_db["app"])
    r = client.get("/api/runs/does-not-exist")
    assert r.status_code == 404


def test_run_detail_endpoint_returns_run(populated_db):
    client = TestClient(populated_db["app"])
    rid = populated_db["run_ids"][0]
    r = client.get(f"/api/runs/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rid
    assert body["prompt"] == "first prompt"


def test_sessions_endpoint_returns_list(populated_db):
    client = TestClient(populated_db["app"])
    r = client.get("/api/sessions")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    ids = {row["id"] for row in body}
    assert ids == set(populated_db["session_ids"])
    # Each row has the expected summary keys.
    for row in body:
        assert {"id", "name", "created_at", "updated_at",
                "entry_count", "is_active"} <= set(row.keys())


def test_sessions_endpoint_respects_limit(populated_db):
    client = TestClient(populated_db["app"])
    r = client.get("/api/sessions", params={"limit": 1})
    assert r.status_code == 200
    assert len(r.json()) == 1


# ── /api/forward-to/{peer} ──────────────────────────────────────────


@pytest.fixture
def app_no_db():
    app = FastAPI()
    app.include_router(router)
    return app


def test_forward_to_unknown_peer_returns_404(app_no_db, monkeypatch):
    monkeypatch.setattr(rest_module, "get_peer_url", lambda name: "")

    client = TestClient(app_no_db)
    r = client.post("/api/forward-to/nope", json={"prompt": "hi"})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown peer"}


def test_forward_to_unreachable_peer_returns_502(app_no_db, monkeypatch):
    monkeypatch.setattr(
        rest_module, "get_peer_url",
        lambda name: "http://127.0.0.1:1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rest_module.httpx, "AsyncClient", _factory)

    client = TestClient(app_no_db)
    r = client.post("/api/forward-to/round_robin", json={"prompt": "hi"})
    assert r.status_code == 502
    body = r.json()
    assert "error" in body
    assert body["error"].startswith("peer unreachable:")


def test_forward_to_happy_path_returns_peer_body(app_no_db, monkeypatch):
    monkeypatch.setattr(
        rest_module, "get_peer_url",
        lambda name: "http://127.0.0.1:9999",
    )

    canned = {"schema_version": "9.9", "prompt": "hi", "enhanced_prompt": "HI!",
              "_marker": "from-peer"}

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json=canned)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rest_module.httpx, "AsyncClient", _factory)

    client = TestClient(app_no_db)
    r = client.post(
        "/api/forward-to/round_robin",
        json={"prompt": "hi", "loop_iteration": 2},
    )

    assert r.status_code == 200
    assert r.json() == canned
    # Forwarded to the peer's /api/enhance.
    assert captured["url"].endswith("/api/enhance")
