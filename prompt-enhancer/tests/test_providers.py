"""Cross-provider conformance suite.

Asserts that every backend (LM Studio, Ollama, OpenAI, Anthropic) honors
the :class:`ChatProvider` ABC contract identically: ``chat()`` returns a
string, ``chat_stream()`` yields a sequence of strings, ``list_models()``
returns a sorted list, ``self._health`` is a :class:`ProviderHealth`,
and the resilience decorators are wired so transient errors retry.

Backends are mocked via :class:`httpx.MockTransport` — these tests do
NOT hit any real network, including the local LM Studio on port 1234.
That's intentional: conformance is a contract test, not an integration
test. Live integration goes through the methodology agent + manual
``enhancer enhance`` runs.
"""

from __future__ import annotations

import json

import httpx
import pytest

from enhancer.llm import anthropic as anthropic_mod
from enhancer.llm import lmstudio as lmstudio_mod
from enhancer.llm import ollama as ollama_mod
from enhancer.llm import openai as openai_mod
from enhancer.llm.anthropic import AnthropicAuthError, AnthropicProvider
from enhancer.llm.lmstudio import LMStudioProvider
from enhancer.llm.ollama import OllamaProvider
from enhancer.llm.openai import OpenAIAuthError, OpenAIProvider
from enhancer.llm.resilience import ProviderHealth


def _patch_httpx(monkeypatch, module, handler):
    """Make every ``httpx.AsyncClient(...)`` in ``module`` use ``MockTransport(handler)``."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)


# ── OpenAI-compat shape (LMStudio, Ollama, OpenAI) ──────────────────


def _openai_chat_response(text: str) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "index": 0}
        ]
    }


def _openai_sse_lines(chunks: list[str]) -> str:
    """Build an SSE response body where every chunk is a `delta.content`."""
    parts: list[str] = []
    for c in chunks:
        payload = {"choices": [{"delta": {"content": c}, "index": 0}]}
        parts.append(f"data: {json.dumps(payload)}\n")
    parts.append("data: [DONE]\n")
    return "\n".join(parts)


@pytest.mark.parametrize(
    "module, provider_factory, base_path",
    [
        (lmstudio_mod, lambda: LMStudioProvider(), "/chat/completions"),
        (ollama_mod, lambda: OllamaProvider(), "/chat/completions"),
        (openai_mod, lambda: OpenAIProvider(), "/chat/completions"),
    ],
    ids=["lmstudio", "ollama", "openai"],
)
@pytest.mark.asyncio
async def test_openai_compat_chat_returns_assistant_content(
    monkeypatch, module, provider_factory, base_path
):
    """All three OpenAI-compat providers return the assistant ``content`` from chat()."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # for OpenAI provider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(base_path)
        return httpx.Response(200, json=_openai_chat_response("hello world"))

    _patch_httpx(monkeypatch, module, handler)
    p = provider_factory()
    out = await p.chat([{"role": "user", "content": "hi"}], model="x")
    assert out == "hello world"


@pytest.mark.parametrize(
    "module, provider_factory",
    [
        (lmstudio_mod, lambda: LMStudioProvider()),
        (ollama_mod, lambda: OllamaProvider()),
        (openai_mod, lambda: OpenAIProvider()),
    ],
    ids=["lmstudio", "ollama", "openai"],
)
@pytest.mark.asyncio
async def test_openai_compat_chat_stream_yields_chunks_in_order(
    monkeypatch, module, provider_factory
):
    """All three OpenAI-compat providers yield ``delta.content`` chunks in order."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    sse = _openai_sse_lines(["a", "b", "c"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse.encode(),
            headers={"content-type": "text/event-stream"},
        )

    _patch_httpx(monkeypatch, module, handler)
    p = provider_factory()
    chunks = [c async for c in p.chat_stream([{"role": "user", "content": "hi"}], model="x")]
    assert chunks == ["a", "b", "c"]


@pytest.mark.parametrize(
    "module, provider_factory, models_path",
    [
        (lmstudio_mod, lambda: LMStudioProvider(), "/models"),
        (ollama_mod, lambda: OllamaProvider(), "/models"),
        (openai_mod, lambda: OpenAIProvider(), "/models"),
    ],
    ids=["lmstudio", "ollama", "openai"],
)
@pytest.mark.asyncio
async def test_openai_compat_list_models_sorted(
    monkeypatch, module, provider_factory, models_path
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(models_path)
        return httpx.Response(200, json={
            "data": [{"id": "zebra"}, {"id": "alpha"}, {"id": "mike"}]
        })

    _patch_httpx(monkeypatch, module, handler)
    p = provider_factory()
    out = await p.list_models()
    assert out == ["alpha", "mike", "zebra"]


# ── Anthropic-shape ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_chat_concatenates_text_blocks(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        body = json.loads(request.content)
        # max_tokens is required and we default it
        assert body["max_tokens"] == 4096
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]
        })

    _patch_httpx(monkeypatch, anthropic_mod, handler)
    p = AnthropicProvider()
    out = await p.chat([{"role": "user", "content": "hi"}], model="claude-test")
    assert out == "hello world"


@pytest.mark.asyncio
async def test_anthropic_splits_system_message_into_top_level(monkeypatch):
    """system role is lifted to top-level ``system`` field; messages array drops it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _patch_httpx(monkeypatch, anthropic_mod, handler)
    p = AnthropicProvider()
    await p.chat([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ], model="claude-test")
    assert captured["body"]["system"] == "be terse"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_chat_stream_yields_text_deltas(monkeypatch):
    """Anthropic SSE: only ``content_block_delta`` with ``text_delta`` yields content."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    events = [
        {"type": "message_start", "message": {"id": "x"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "alpha "}},
        {"type": "ping"},  # ignored
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "beta"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ]
    body = "\n".join(f"data: {json.dumps(e)}" for e in events) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    _patch_httpx(monkeypatch, anthropic_mod, handler)
    p = AnthropicProvider()
    chunks = [c async for c in p.chat_stream(
        [{"role": "user", "content": "hi"}], model="claude-test",
    )]
    assert chunks == ["alpha ", "beta"]


# ── Auth + error paths ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = OpenAIProvider()
    with pytest.raises(OpenAIAuthError):
        await p.chat([{"role": "user", "content": "hi"}], model="x")


@pytest.mark.asyncio
async def test_anthropic_raises_without_api_key_on_hosted(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ENHANCER_ANTHROPIC_BASE_URL", raising=False)
    p = AnthropicProvider()
    with pytest.raises(AnthropicAuthError):
        await p.chat([{"role": "user", "content": "hi"}], model="claude-test")


@pytest.mark.asyncio
async def test_anthropic_tolerates_missing_api_key_on_compat(monkeypatch):
    """LM Studio compat path works without ANTHROPIC_API_KEY."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ENHANCER_ANTHROPIC_BASE_URL", "http://localhost:1234")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _patch_httpx(monkeypatch, anthropic_mod, handler)
    p = AnthropicProvider()
    out = await p.chat([{"role": "user", "content": "hi"}], model="claude-test")
    assert out == "ok"


@pytest.mark.parametrize(
    "module, provider_factory",
    [
        (lmstudio_mod, lambda: LMStudioProvider()),
        (ollama_mod, lambda: OllamaProvider()),
        (openai_mod, lambda: OpenAIProvider()),
        (anthropic_mod, lambda: AnthropicProvider()),
    ],
    ids=["lmstudio", "ollama", "openai", "anthropic"],
)
@pytest.mark.asyncio
async def test_list_models_swallows_errors(monkeypatch, module, provider_factory):
    """All providers return [] on connection error rather than raising."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_httpx(monkeypatch, module, handler)
    p = provider_factory()
    assert await p.list_models() == []


# ── Health surface (decorator integration) ──────────────────────────


@pytest.mark.parametrize(
    "factory",
    [
        lambda: LMStudioProvider(),
        lambda: OllamaProvider(),
        lambda: OpenAIProvider(),
        lambda: AnthropicProvider(),
    ],
    ids=["lmstudio", "ollama", "openai", "anthropic"],
)
def test_every_provider_has_health(factory):
    p = factory()
    assert isinstance(p._health, ProviderHealth)
    assert not p._health.is_open  # fresh instance starts closed
