"""Tests for ``enhancer.api.rest`` — POST /api/enhance + health.

Uses FastAPI's TestClient against a router that's been wired to a
fake provider via monkeypatch. Asserts the returned envelope matches
the documented schema and includes real Pass 4 scores (not P4_DEFAULTS).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enhancer.api import rest as rest_module
from enhancer.api.rest import router


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
    assert "interpreter" in body["services"]


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
