"""Tests for the /api/persona-handoff endpoints (POST/GET/DELETE).

Wire-format contract is fixed across siblings — see
``round-robin/CLAUDE.md`` and the prompt-enhancer handoff client. Any change
here must be coordinated on both sides.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from round_robin import discovery
from round_robin import config as rr_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Same isolation pattern as test_endpoints.py so persisted state /
    # services.toml from the host don't leak in.
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


def test_post_persona_handoff_stores(client):
    payload = {
        "theme": "Discuss API design",
        "alpha_persona": "You are a pragmatic engineer.",
        "bravo_persona": "You are a security-minded reviewer.",
        "source": "prompt-enhancer",
    }
    r = client.post("/api/persona-handoff", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["stored_at"], str)
    assert body["stored_at"].endswith("Z")

    # GET must return what was stored, plus stored_at.
    r2 = client.get("/api/persona-handoff")
    assert r2.status_code == 200
    got = r2.json()
    assert got["theme"] == payload["theme"]
    assert got["alpha_persona"] == payload["alpha_persona"]
    assert got["bravo_persona"] == payload["bravo_persona"]
    assert got["source"] == "prompt-enhancer"
    assert got["stored_at"] == body["stored_at"]


def test_post_rejects_empty_theme(client):
    r = client.post("/api/persona-handoff", json={
        "theme": "",
        "alpha_persona": "a",
        "bravo_persona": "b",
    })
    assert r.status_code == 400
    body = r.json()
    assert "theme" in body["detail"].lower()


def test_post_rejects_whitespace_only_theme(client):
    # Whitespace-only is treated as empty (theme.strip() check).
    r = client.post("/api/persona-handoff", json={
        "theme": "   \n\t",
        "alpha_persona": "",
        "bravo_persona": "",
    })
    assert r.status_code == 400


def test_get_returns_204_when_empty(client):
    # Fresh server: nothing has been POSTed yet.
    r = client.get("/api/persona-handoff")
    assert r.status_code == 204
    # 204 must not carry a body
    assert r.content == b""


def test_delete_clears_handoff(client):
    client.post("/api/persona-handoff", json={
        "theme": "T",
        "alpha_persona": "A",
        "bravo_persona": "B",
    })
    # Sanity: it's there
    assert client.get("/api/persona-handoff").status_code == 200

    r = client.delete("/api/persona-handoff")
    assert r.status_code == 204
    assert r.content == b""

    # Now empty again
    assert client.get("/api/persona-handoff").status_code == 204


def test_concurrent_post_overwrites(client):
    # Last-write-wins is fine for this ephemeral one-shot store.
    client.post("/api/persona-handoff", json={
        "theme": "first",
        "alpha_persona": "A1",
        "bravo_persona": "B1",
    })
    client.post("/api/persona-handoff", json={
        "theme": "second",
        "alpha_persona": "A2",
        "bravo_persona": "B2",
    })
    got = client.get("/api/persona-handoff").json()
    assert got["theme"] == "second"
    assert got["alpha_persona"] == "A2"
    assert got["bravo_persona"] == "B2"


def test_source_defaults_to_prompt_enhancer(client):
    # Source omitted -> must default to "prompt-enhancer".
    r = client.post("/api/persona-handoff", json={
        "theme": "T",
        "alpha_persona": "",
        "bravo_persona": "",
    })
    assert r.status_code == 200
    got = client.get("/api/persona-handoff").json()
    assert got["source"] == "prompt-enhancer"


def test_post_accepts_empty_personas(client):
    # alpha_persona / bravo_persona may be empty (only theme is required).
    r = client.post("/api/persona-handoff", json={
        "theme": "Open-ended discussion",
        "alpha_persona": "",
        "bravo_persona": "",
    })
    assert r.status_code == 200
    got = client.get("/api/persona-handoff").json()
    assert got["theme"] == "Open-ended discussion"
    assert got["alpha_persona"] == ""
    assert got["bravo_persona"] == ""
