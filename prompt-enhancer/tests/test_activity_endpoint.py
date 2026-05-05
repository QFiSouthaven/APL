"""Tests for ``GET /api/activity`` and the activity ring buffer.

Verifies the cross-umbrella wire contract: same shape across all three
siblings, newest-first, ring-buffered, ephemeral, limit clamped.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enhancer.api import activity as activity_module
from enhancer.api import rest as rest_module
from enhancer.api.rest import router


# Canned LLM responses to drive a clean run end-to-end.
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


@pytest.fixture(autouse=True)
def _clear_activity_buffer():
    """Each test starts with an empty ring buffer."""
    activity_module.clear()
    yield
    activity_module.clear()


@pytest.fixture
def fake_app(fake_provider, monkeypatch, tmp_path):
    fake_provider.stream_responses.extend([
        PASS1_TOKENS, PASS2_TOKENS, PASS3_TOKENS, PASS4_TOKENS,
    ])
    fake_provider.available_models = ["fake-7b"]
    monkeypatch.setattr(rest_module, "get_provider", lambda settings: fake_provider)
    monkeypatch.setattr(rest_module, "db_path", lambda: tmp_path / "enhancer.db")
    monkeypatch.setattr(rest_module, "jsonl_log_path", lambda: tmp_path / "events.jsonl")
    app = FastAPI()
    app.include_router(router)
    return app


def test_activity_empty_buffer_returns_200_with_empty_events(fake_app):
    client = TestClient(fake_app)
    r = client.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "prompt_enhancer"
    assert body["events"] == []


def test_activity_records_events_after_enhance_run(fake_app):
    client = TestClient(fake_app)
    r = client.post("/api/enhance", json={"prompt": "test prompt"})
    assert r.status_code == 200

    r = client.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "prompt_enhancer"
    events = body["events"]
    assert len(events) > 0
    # We expect at least: run_started + 4 pass_results + 1 run_done.
    types = [e["type"] for e in events]
    assert "run_started" in types
    assert "pass_result" in types
    assert "run_done" in types

    # Wire shape: every event has ts (Z-suffixed), type, summary.
    for ev in events:
        assert "ts" in ev and ev["ts"].endswith("Z")
        assert "type" in ev and isinstance(ev["type"], str)
        assert "summary" in ev and isinstance(ev["summary"], str)
        assert len(ev["summary"]) <= 120


def test_activity_orders_newest_first(fake_app):
    activity_module.record("a", "first")
    activity_module.record("b", "second")
    activity_module.record("c", "third")

    client = TestClient(fake_app)
    r = client.get("/api/activity")
    events = r.json()["events"]
    summaries = [e["summary"] for e in events]
    assert summaries == ["third", "second", "first"]


def test_activity_limit_param(fake_app):
    for i in range(60):
        activity_module.record("x", f"event-{i}")

    client = TestClient(fake_app)
    # Default 50.
    r = client.get("/api/activity")
    assert len(r.json()["events"]) == 50

    # Explicit 1.
    r = client.get("/api/activity?limit=1")
    assert len(r.json()["events"]) == 1

    # Explicit 200.
    r = client.get("/api/activity?limit=200")
    # Buffer caps at 200 so we should get 60 (everything we recorded).
    assert len(r.json()["events"]) == 60

    # >200 clamped — FastAPI Query rejects with 422 (le=200 enforced).
    r = client.get("/api/activity?limit=500")
    assert r.status_code == 422


def test_activity_ring_buffer_caps_at_200(fake_app):
    # Push way past the cap.
    for i in range(500):
        activity_module.record("x", f"event-{i}")
    client = TestClient(fake_app)
    r = client.get("/api/activity?limit=200")
    events = r.json()["events"]
    # At most 200 retained.
    assert len(events) == 200
    # Newest-first means event-499 is at the top.
    assert events[0]["summary"] == "event-499"
    assert events[-1]["summary"] == "event-300"


def test_activity_summary_truncated_to_120_chars(fake_app):
    long = "x" * 500
    activity_module.record("x", long)
    client = TestClient(fake_app)
    body = client.get("/api/activity").json()
    summary = body["events"][0]["summary"]
    assert len(summary) == 120
    assert summary.endswith("...")


def test_activity_record_persona_handoff(fake_app):
    activity_module.record_persona_handoff(
        "round_robin", "AI ethics", "Persona A text", "Persona B text"
    )
    client = TestClient(fake_app)
    body = client.get("/api/activity").json()
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["type"] == "persona_handoff"
    assert "round_robin" in ev["summary"]
    assert "alpha=" in ev["summary"]
    assert "bravo=" in ev["summary"]
