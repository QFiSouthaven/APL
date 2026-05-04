"""Ollama provider — OpenAI-compatible backend at ``localhost:11434``.

Mirrors :class:`LMStudioProvider` since Ollama's ``/v1/chat/completions``
endpoint follows the same OpenAI-compatible SSE shape. Native Ollama
quirks (``/api/generate``, ``/api/tags``) are intentionally NOT used —
keeping the provider code identical to LM Studio means the resilience
decorators, parsing, and budgeting all behave the same way.

Configurable via env vars (read by :class:`enhancer.config.Settings`):

* ``ENHANCER_OLLAMA_BASE_URL`` — default ``http://localhost:11434/v1``
* per-call ``timeout`` and ``idle_timeout`` honored same as LM Studio
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from .base import ChatProvider
from .resilience import ProviderHealth, with_retry, with_stream_retry

logger = logging.getLogger("enhancer.llm.ollama")


class OllamaProvider(ChatProvider):
    """Ollama backend (OpenAI-compatible)."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        default_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout = default_timeout
        self._health = ProviderHealth()

    # ── discovery ───────────────────────────────────────────────────

    async def list_models(self) -> list[str]:
        """List models Ollama has pulled.

        Uses ``/v1/models`` (OpenAI-compat). Errors swallowed so callers
        can render an empty list rather than crash.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/models")
                resp.raise_for_status()
                data = resp.json().get("data", [])
                return sorted({m["id"] for m in data if m.get("id")})
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("list_models failed: %s", exc)
            return []

    # ── completion ──────────────────────────────────────────────────

    @with_retry()
    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> str:
        """Non-streaming chat completion."""
        body: dict = {"model": model, "messages": messages, "stream": False}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    @with_stream_retry()
    async def chat_stream(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        idle_timeout: float = 120.0,
    ) -> AsyncIterator[str]:
        """Streaming chat completion via SSE.

        ``idle_timeout`` is the per-chunk read timeout — same load-bearing
        protection as LM Studio against a stalled remote-GPU stream.
        """
        body: dict = {"model": model, "messages": messages, "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature

        stream_timeout = httpx.Timeout(timeout, connect=15.0, read=idle_timeout)

        async with httpx.AsyncClient(timeout=stream_timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
