"""Tests for the discovery / introspection endpoints on the FastAPI app.

Covers ``GET /api/peers`` and the discovery-shaped fields added to
``GET /api/health`` in v1.2.0. These are the surfaces sibling products
(prompt-enhancer, development, swarm-loop) call for cross-product
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
    assert body["services"]["development"] == "http://127.0.0.1:8767"


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


# ── /api/review (code-review dialogue, consumed by development) ─────────


@pytest.fixture
def review_client(tmp_path, monkeypatch):
    """Same isolation as ``client`` but with the code-review dialogue stubbed.

    Returns ``(test_client, set_responses)``: ``set_responses(list)`` swaps
    the canned LM responses the dialogue will see. By default the patched
    function returns a happy-path approve verdict so a test that doesn't
    care about specific dialogue output still gets a 200.
    """
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

    # Patch the review function the route imports so we don't touch LM Studio.
    state: dict = {"impl": None}

    async def _fake_review(layer, purpose, files, *, lm_client=None, model=None,
                            **_kwargs):
        impl = state["impl"]
        if impl is None:
            return {
                "approved": True,
                "issues": [],
                "request_regenerate": False,
                "agents": {
                    "agent_a_verdict": "stub-A",
                    "agent_b_verdict": "stub-B",
                    "consensus": "stubbed approve",
                },
            }
        return await impl(layer, purpose, files, lm_client=lm_client, model=model)

    monkeypatch.setattr(srv, "review_with_dialogue", _fake_review)

    app = srv.create_app()

    def set_impl(impl):
        state["impl"] = impl

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, set_impl


def test_review_200_returns_contract_shape(review_client):
    c, _ = review_client
    r = c.post("/api/review", json={
        "layer": "core",
        "purpose": "do core things",
        "files": {"a.py": "x = 1"},
    })
    assert r.status_code == 200
    body = r.json()
    # Contract keys (load-bearing for development.RoundRobinReviewer).
    assert "approved" in body
    assert "issues" in body
    assert "request_regenerate" in body
    # Round-robin extra metadata.
    assert "agents" in body
    assert "agent_a_verdict" in body["agents"]
    assert "agent_b_verdict" in body["agents"]
    assert "consensus" in body["agents"]


def test_review_422_on_missing_layer(review_client):
    c, _ = review_client
    r = c.post("/api/review", json={
        "purpose": "p", "files": {"a.py": "."},
    })
    assert r.status_code == 422


def test_review_422_on_missing_purpose(review_client):
    c, _ = review_client
    r = c.post("/api/review", json={
        "layer": "core", "files": {"a.py": "."},
    })
    assert r.status_code == 422


def test_review_422_on_missing_files(review_client):
    c, _ = review_client
    r = c.post("/api/review", json={"layer": "core", "purpose": "p"})
    assert r.status_code == 422


def test_review_422_on_empty_files_dict(review_client):
    c, _ = review_client
    r = c.post("/api/review", json={
        "layer": "core", "purpose": "p", "files": {},
    })
    assert r.status_code == 422


def test_review_503_when_dialogue_raises(review_client):
    c, set_impl = review_client

    async def _boom(layer, purpose, files, **kwargs):
        raise RuntimeError("LM Studio unreachable: simulated")

    set_impl(_boom)
    r = c.post("/api/review", json={
        "layer": "core",
        "purpose": "p",
        "files": {"a.py": "x = 1"},
    })
    assert r.status_code == 503
    body = r.json()
    assert "error" in body
    assert isinstance(body["error"], str)
    assert "review unavailable" in body["error"]


def test_review_response_always_has_three_contract_keys(review_client):
    c, set_impl = review_client

    async def _impl(layer, purpose, files, **kwargs):
        return {
            "approved": False,
            "issues": ["i1"],
            "request_regenerate": True,
            "agents": {"agent_a_verdict": "a", "agent_b_verdict": "b",
                        "consensus": "c"},
        }

    set_impl(_impl)
    r = c.post("/api/review", json={
        "layer": "core", "purpose": "p", "files": {"a.py": "."},
    })
    assert r.status_code == 200
    body = r.json()
    # All four keys present (3 contract + 1 metadata).
    assert set(body.keys()) >= {"approved", "issues", "request_regenerate", "agents"}
    assert body["approved"] is False
    assert body["issues"] == ["i1"]
    assert body["request_regenerate"] is True
