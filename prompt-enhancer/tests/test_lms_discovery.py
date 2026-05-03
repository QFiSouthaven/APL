"""Tests for `enhancer.llm.lms_discovery`."""

from __future__ import annotations

import httpx
import pytest

from enhancer.llm import lms_discovery
from enhancer.llm.lms_discovery import (
    ModelInfo,
    ModelLoadUnavailableError,
    discover_chat_models,
    ensure_model_loaded,
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
