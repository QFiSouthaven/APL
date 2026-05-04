"""Tests for `enhancer.llm.lms_discovery`."""

from __future__ import annotations

import httpx
import pytest

from enhancer.llm import lms_discovery
from enhancer.llm.lms_discovery import (
    ModelInfo,
    ModelLoadUnavailableError,
    discover_chat_models,
    discover_chat_models_multihost,
    ensure_model_loaded,
    pick_loaded_host,
)


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler):
    """Make every `httpx.AsyncClient(...)` use `httpx.MockTransport(handler)`."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(lms_discovery.httpx, "AsyncClient", _factory)


# ── discover_chat_models ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_filters_non_chat_and_sorts_loaded_first(monkeypatch):
    payload = {
        "data": [
            {"id": "embed-v1", "type": "embeddings", "state": "not-loaded"},
            {"id": "qwen-vl", "type": "vlm", "state": "not-loaded"},
            {"id": "llama-loaded", "type": "llm", "state": "loaded",
             "max_context_length": 8192, "loaded_context_length": 4096},
            {"id": "weird-model", "type": "unknown", "state": "loaded"},
            {"id": "alpha-llm", "type": "llm", "state": "not-loaded"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v0/models"
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)

    out = await discover_chat_models("http://localhost:1234")
    ids = [m.id for m in out]

    # Non-chat types filtered out.
    assert "embed-v1" not in ids
    assert "weird-model" not in ids
    # Chat-capable types kept.
    assert set(ids) == {"qwen-vl", "llama-loaded", "alpha-llm"}
    # Loaded first.
    assert out[0].id == "llama-loaded"
    assert out[0].is_loaded is True
    assert out[0].loaded_context == 4096
    # Tail alphabetic.
    assert out[1].id == "alpha-llm"
    assert out[2].id == "qwen-vl"


@pytest.mark.asyncio
async def test_discover_returns_empty_on_connection_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    _patch_httpx(monkeypatch, handler)
    out = await discover_chat_models("http://localhost:1234")
    assert out == []


@pytest.mark.asyncio
async def test_discover_returns_empty_on_non_2xx(monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"error": "internal"})

    _patch_httpx(monkeypatch, handler)
    out = await discover_chat_models("http://localhost:1234")
    assert out == []


# ── ensure_model_loaded ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_returns_preferred_when_loaded(monkeypatch):
    payload = {
        "data": [
            {"id": "llama-A", "type": "llm", "state": "loaded"},
            {"id": "llama-B", "type": "llm", "state": "loaded"},
        ]
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    chosen = await ensure_model_loaded(preferred="llama-B", base_url="http://localhost:1234")
    assert chosen == "llama-B"


@pytest.mark.asyncio
async def test_ensure_returns_first_loaded_when_no_preferred(monkeypatch):
    payload = {
        "data": [
            {"id": "alpha", "type": "llm", "state": "not-loaded"},
            {"id": "zebra", "type": "llm", "state": "loaded"},
        ]
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    chosen = await ensure_model_loaded(base_url="http://localhost:1234")
    # zebra is loaded → comes first in sorted output.
    assert chosen == "zebra"


@pytest.mark.asyncio
async def test_ensure_loads_when_nothing_loaded(monkeypatch):
    """If nothing is loaded, ensure_model_loaded calls _load_via_cli
    then re-polls. We mock _load_via_cli to succeed and second poll
    to return a loaded model."""
    poll_count = {"n": 0}

    def handler(request):
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            # First call: nothing loaded.
            return httpx.Response(200, json={
                "data": [{"id": "alpha", "type": "llm", "state": "not-loaded"}]
            })
        # Second call after load: alpha now loaded.
        return httpx.Response(200, json={
            "data": [{"id": "alpha", "type": "llm", "state": "loaded"}]
        })

    _patch_httpx(monkeypatch, handler)

    async def fake_load(model_id, timeout=90.0):
        return True, ""

    monkeypatch.setattr(lms_discovery, "_load_via_cli", fake_load)
    chosen = await ensure_model_loaded(base_url="http://localhost:1234")
    assert chosen == "alpha"
    assert poll_count["n"] == 2  # one before load, one after


@pytest.mark.asyncio
async def test_ensure_raises_when_lms_unreachable(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(ModelLoadUnavailableError) as ei:
        await ensure_model_loaded(base_url="http://localhost:1234")
    assert "unreachable" in str(ei.value).lower() or "no chat-capable" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_ensure_raises_when_load_fails(monkeypatch):
    """All chat models present but none loaded; lms load returns non-zero."""
    def handler(request):
        return httpx.Response(200, json={
            "data": [{"id": "alpha", "type": "llm", "state": "not-loaded"}]
        })

    _patch_httpx(monkeypatch, handler)

    async def fake_load(model_id, timeout=90.0):
        return False, "rc=1: out of memory"

    monkeypatch.setattr(lms_discovery, "_load_via_cli", fake_load)
    with pytest.raises(ModelLoadUnavailableError) as ei:
        await ensure_model_loaded(base_url="http://localhost:1234")
    msg = str(ei.value)
    assert "out of memory" in msg
    assert "Open LM Studio" in msg


@pytest.mark.asyncio
async def test_ensure_raises_when_load_succeeds_but_repoll_empty(monkeypatch):
    """`lms load` exits 0 but the model never shows loaded — surface this
    as an error rather than silently succeeding."""
    def handler(request):
        return httpx.Response(200, json={
            "data": [{"id": "alpha", "type": "llm", "state": "not-loaded"}]
        })

    _patch_httpx(monkeypatch, handler)

    async def fake_load(model_id, timeout=90.0):
        return True, ""

    monkeypatch.setattr(lms_discovery, "_load_via_cli", fake_load)
    with pytest.raises(ModelLoadUnavailableError) as ei:
        await ensure_model_loaded(base_url="http://localhost:1234")
    assert "no model is reporting loaded" in str(ei.value)


# ── ModelInfo dataclass ─────────────────────────────────────────────


def test_modelinfo_is_loaded_property():
    assert ModelInfo(id="x", type="llm", state="loaded").is_loaded is True
    assert ModelInfo(id="x", type="llm", state="not-loaded").is_loaded is False


# ── multi-host discovery ────────────────────────────────────────────


def _patch_httpx_by_host(monkeypatch: pytest.MonkeyPatch, host_handlers: dict):
    """Patch httpx so the request URL's authority routes to a per-host
    handler. ``host_handlers`` maps a substring of the request URL
    (e.g. ``"localhost:1234"`` or ``"192.168.1.50"``) to a handler
    callable. A handler may raise to simulate connection failure.
    """
    real_client = httpx.AsyncClient

    def make_router(handlers):
        def router(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            for needle, handler in handlers.items():
                if needle in url_str:
                    return handler(request)
            raise httpx.ConnectError(f"unrouted: {url_str}")
        return router

    transport = httpx.MockTransport(make_router(host_handlers))

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(lms_discovery.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_multihost_aggregates_two_hosts(monkeypatch):
    def host_a(request):
        return httpx.Response(200, json={
            "data": [
                {"id": "alpha-loaded", "type": "llm", "state": "loaded"},
                {"id": "alpha-spare", "type": "llm", "state": "not-loaded"},
            ]
        })

    def host_b(request):
        return httpx.Response(200, json={
            "data": [
                {"id": "beta-only", "type": "llm", "state": "not-loaded"},
            ]
        })

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
        "192.168.1.50:1234": host_b,
    })

    out = await discover_chat_models_multihost([
        "http://localhost:1234",
        "http://192.168.1.50:1234",
    ])

    assert set(out.keys()) == {
        "http://localhost:1234",
        "http://192.168.1.50:1234",
    }
    a_ids = [m.id for m in out["http://localhost:1234"]]
    b_ids = [m.id for m in out["http://192.168.1.50:1234"]]
    # Loaded-first ordering preserved per-host.
    assert a_ids == ["alpha-loaded", "alpha-spare"]
    assert b_ids == ["beta-only"]


@pytest.mark.asyncio
async def test_multihost_one_host_down_returns_other_data(monkeypatch):
    def host_a(request):
        raise httpx.ConnectError("refused")

    def host_b(request):
        return httpx.Response(200, json={
            "data": [{"id": "survivor", "type": "llm", "state": "loaded"}]
        })

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
        "192.168.1.50:1234": host_b,
    })

    out = await discover_chat_models_multihost([
        "http://localhost:1234",
        "http://192.168.1.50:1234",
    ])

    assert out["http://localhost:1234"] == []
    assert [m.id for m in out["http://192.168.1.50:1234"]] == ["survivor"]


@pytest.mark.asyncio
async def test_multihost_empty_input_returns_empty_dict(monkeypatch):
    out = await discover_chat_models_multihost([])
    assert out == {}


# ── pick_loaded_host ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_loaded_host_honors_preferred(monkeypatch):
    def host_a(request):
        return httpx.Response(200, json={
            "data": [{"id": "common-id", "type": "llm", "state": "loaded"}]
        })

    def host_b(request):
        return httpx.Response(200, json={
            "data": [{"id": "wanted", "type": "llm", "state": "loaded"}]
        })

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
        "192.168.1.50:1234": host_b,
    })

    host, model = await pick_loaded_host(
        ["http://localhost:1234", "http://192.168.1.50:1234"],
        preferred_model="wanted",
    )
    assert host == "http://192.168.1.50:1234"
    assert model == "wanted"


@pytest.mark.asyncio
async def test_pick_loaded_host_falls_back_to_first_loaded(monkeypatch):
    """No preferred → first host (in input order) with any loaded chat
    model wins, even if a later host has more models."""
    def host_a(request):
        return httpx.Response(200, json={
            "data": [{"id": "a-loaded", "type": "llm", "state": "loaded"}]
        })

    def host_b(request):
        return httpx.Response(200, json={
            "data": [
                {"id": "b1-loaded", "type": "llm", "state": "loaded"},
                {"id": "b2-loaded", "type": "llm", "state": "loaded"},
            ]
        })

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
        "192.168.1.50:1234": host_b,
    })

    host, model = await pick_loaded_host([
        "http://localhost:1234",
        "http://192.168.1.50:1234",
    ])
    assert host == "http://localhost:1234"
    assert model == "a-loaded"


@pytest.mark.asyncio
async def test_pick_loaded_host_skips_unloaded_hosts(monkeypatch):
    """First host has only unloaded models; pick_loaded_host should move
    on to the second host."""
    def host_a(request):
        return httpx.Response(200, json={
            "data": [{"id": "cold", "type": "llm", "state": "not-loaded"}]
        })

    def host_b(request):
        return httpx.Response(200, json={
            "data": [{"id": "hot", "type": "llm", "state": "loaded"}]
        })

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
        "192.168.1.50:1234": host_b,
    })

    host, model = await pick_loaded_host([
        "http://localhost:1234",
        "http://192.168.1.50:1234",
    ])
    assert host == "http://192.168.1.50:1234"
    assert model == "hot"


@pytest.mark.asyncio
async def test_pick_loaded_host_returns_none_when_nothing_loaded(monkeypatch):
    def host_a(request):
        return httpx.Response(200, json={
            "data": [{"id": "cold-a", "type": "llm", "state": "not-loaded"}]
        })

    def host_b(request):
        raise httpx.ConnectError("refused")

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
        "192.168.1.50:1234": host_b,
    })

    host, model = await pick_loaded_host([
        "http://localhost:1234",
        "http://192.168.1.50:1234",
    ])
    assert host is None
    assert model is None


@pytest.mark.asyncio
async def test_pick_loaded_host_preferred_not_loaded_falls_back(monkeypatch):
    """Preferred model exists somewhere but is not loaded → fall back to
    the first-loaded rule."""
    def host_a(request):
        return httpx.Response(200, json={
            "data": [
                {"id": "wanted", "type": "llm", "state": "not-loaded"},
                {"id": "alt", "type": "llm", "state": "loaded"},
            ]
        })

    _patch_httpx_by_host(monkeypatch, {
        "localhost:1234": host_a,
    })

    host, model = await pick_loaded_host(
        ["http://localhost:1234"],
        preferred_model="wanted",
    )
    # Preferred not loaded → fallback returns the first loaded model.
    assert host == "http://localhost:1234"
    assert model == "alt"
