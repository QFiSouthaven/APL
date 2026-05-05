"""Tests for round-robin's ``GET /api/activity`` endpoint.

Same wire shape as prompt-enhancer + development. Verifies persona
handoff is recorded, ring buffer is bounded, ordering is newest-first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from round_robin import activity as activity_module
from round_robin import config as rr_config
from round_robin import discovery


@pytest.fixture(autouse=True)
def _clear_activity_buffer():
    activity_module.clear()
    yield
    activity_module.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake_toml = tmp_path / "services.toml"
    monkeypatch.setattr(discovery, "services_path", lambda: fake_toml)
    state_file = tmp_path / "state.json"
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(rr_config, "STATE_FILE", state_file)
    monkeypatch.setattr(rr_config, "CONFIG_FILE", config_file)
    import round_robin.server as srv
    monkeypatch.setattr(srv, "STATE_FILE", state_file)
    import round_robin.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "STATE_FILE", state_file)
    app = srv.create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_activity_empty_returns_200(client):
    r = client.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "round_robin"
    assert body["events"] == []


def test_activity_records_persona_handoff(client):
    body = {
        "theme": "AI ethics in 2026",
        "alpha_persona": "A skeptical researcher.",
        "bravo_persona": "An optimistic engineer.",
        "source": "prompt-enhancer",
    }
    r = client.post("/api/persona-handoff", json=body)
    assert r.status_code == 200

    r = client.get("/api/activity")
    assert r.status_code == 200
    payload = r.json()
    assert payload["service"] == "round_robin"
    events = payload["events"]
    assert len(events) >= 1
    # Persona handoff event surfaces with source + theme size.
    handoff = [e for e in events if e["type"] == "persona_handoff"]
    assert len(handoff) == 1
    assert "prompt-enhancer" in handoff[0]["summary"]
    assert "theme=" in handoff[0]["summary"]


def test_activity_wire_shape(client):
    activity_module.record("test_event", "hello world")
    body = client.get("/api/activity").json()
    ev = body["events"][0]
    assert "ts" in ev and ev["ts"].endswith("Z")
    assert ev["type"] == "test_event"
    assert ev["summary"] == "hello world"
    assert len(ev["summary"]) <= 120


def test_activity_orders_newest_first(client):
    activity_module.record("a", "first")
    activity_module.record("b", "second")
    activity_module.record("c", "third")

    body = client.get("/api/activity").json()
    summaries = [e["summary"] for e in body["events"]]
    assert summaries == ["third", "second", "first"]


def test_activity_limit_clamped(client):
    for i in range(60):
        activity_module.record("x", f"event-{i}")
    # default 50
    assert len(client.get("/api/activity").json()["events"]) == 50
    # explicit 1
    assert len(client.get("/api/activity?limit=1").json()["events"]) == 1
    # >200 silently clamped (round-robin doesn't 422 — Studio panel
    # treats as best-effort).
    body = client.get("/api/activity?limit=500").json()
    assert len(body["events"]) <= 200


def test_record_emit_translates_run_started():
    activity_module.record_emit(
        "run_started",
        {
            "run_id": "rr-1",
            "config": {
                "theme": "the future",
                "agents": [{"name": "Alpha"}, {"name": "Bravo"}],
            },
        },
    )
    snap = activity_module.snapshot()
    assert len(snap) == 1
    ev = snap[0]
    assert ev["type"] == "run_started"
    assert "Alpha" in ev["summary"]
    assert "Bravo" in ev["summary"]
    assert "the future" in ev["summary"]


def test_record_emit_translates_turn_done():
    activity_module.record_emit(
        "turn_done",
        {"turn": 2, "agent_name": "Alpha", "content": "Hello world"},
    )
    snap = activity_module.snapshot()
    assert len(snap) == 1
    assert snap[0]["type"] == "turn_done"
    assert "Turn 2" in snap[0]["summary"]
    assert "Alpha" in snap[0]["summary"]


def test_record_emit_ignores_uninteresting_events():
    activity_module.record_emit("turn_chunk", {"turn": 1, "token": "x"})
    activity_module.record_emit("run_paused", {})
    # No interesting events => empty snapshot.
    assert activity_module.snapshot() == []
