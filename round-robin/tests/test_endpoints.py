"""Tests for the discovery / introspection endpoints on the FastAPI app.

Covers ``GET /api/peers`` and the discovery-shaped fields added to
``GET /api/health`` in v1.2.0. These are the surfaces sibling products
(prompt-enhancer, interpreter, swarm-loop) call for cross-product
service introspection.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from round_robin import __version__, discovery
from round_robin import config as rr_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate services.toml so the test environment doesn't leak into us.
    fake_toml = tmp_path / "services.toml"
    monkeypatch.setattr(discovery, "services_path", lambda: fake_toml)

    # Isolate state/config like test_server_routes does.
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
        yield c, fake_toml


def test_peers_returns_defaults_with_no_file(client):
    c, _ = client
    r = c.get("/api/peers")
    assert r.status_code == 200
    body = r.json()
    # Shape: byte-for-byte match with prompt_enhancer.api.rest.peers
    assert "services" in body
    assert body["services"]["prompt_enhancer"] == "http://127.0.0.1:8765"
    assert body["services"]["round_robin"] == "http://127.0.0.1:8766"
    assert body["services"]["interpreter"] == "http://127.0.0.1:8767"


def test_peers_reflects_toml_overrides(client):
    c, fake_toml = client
    fake_toml.write_text(
        "[services]\nprompt_enhancer = \"http://my-host:9000\"\n",
        encoding="utf-8",
    )
    body = c.get("/api/peers").json()
    assert body["services"]["prompt_enhancer"] == "http://my-host:9000"
    # Other peers still resolve to defaults.
    assert body["services"]["round_robin"] == "http://127.0.0.1:8766"


def test_health_includes_discovery_fields(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    # New fields added in v1.2.0 for prompt-enhancer's introspection.
    assert body["status"] == "ok"
    assert body["service"] == "round_robin"
    assert body["version"] == __version__
    # Existing fields used by the desktop UI must still be present.
    assert "reachable" in body
    assert "models" in body


def test_peers_endpoint_uses_get(client):
    c, _ = client
    # Wrong method should not 200.
    r = c.post("/api/peers")
    assert r.status_code in (404, 405)
