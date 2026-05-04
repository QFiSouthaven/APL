"""LM Studio (OpenAI-compatible) provider.

Lifted from ``swarm-agent-dev/src/webui/services/lmstudio.py`` and wrapped
in the :class:`ChatProvider` ABC. The httpx + SSE machinery is preserved
verbatim — including the carefully-tuned ``idle_timeout=120.0`` default
which protects against LM Link silently stalling a slow remote-GPU
stream.

Three endpoints are used:

* ``{base_url}/chat/completions`` — OpenAI-compatible chat (inference).
  Used by ``chat`` and ``chat_stream``.
* ``{base_url}/chat/completions`` with a ``tools`` array — OpenAI-
  compatible function-calling. Used by ``chat_with_tools``; returns
  OpenAI-shaped ``{content, tool_calls}`` rather than a plain string.
* ``{management_url}/api/v0/models`` — LM Studio mgmt API for context
  windows + richer model metadata (used by ``context_window`` and the
  budgeting layer).

Both ``/v1/models`` (OpenAI-style) and ``/api/v0/models`` (LM Studio-
specific) are queried for the model list — they sometimes differ when
LM Link is bridging remote models.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from .base import ChatProvider
from .lms_link import get_active_base_url
from .resilience import ProviderHealth, with_retry, with_stream_retry

logger = logging.getLogger("enhancer.llm.lmstudio")


class LMStudioProvider(ChatProvider):
    """LM Studio + LM Link backend (OpenAI-compatible)."""

    name = "lmstudio"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234/v1",
        management_url: str = "http://localhost:1234",
        default_timeout: float = 120.0,
    ) -> None:
        # Save the configured default; runtime override (via lms_link)
        # is checked on every call so `enhancer host use <url>` takes
        # effect without restarting.
        self._default_base_url = base_url.rstrip("/")
        self.management_url = management_url.rstrip("/")
        self.default_timeout = default_timeout
        self._health = ProviderHealth()

    @property
    def base_url(self) -> str:
        """Active base URL — override if set, else the configured default."""
        return get_active_base_url(self._default_base_url)

    # ── discovery ───────────────────────────────────────────────────

    async def list_models(self) -> list[str]:
        """List models LM Studio currently exposes.

        Uses ``/v1/models``; returns sorted unique IDs. Errors are swallowed
        so callers can render a friendly empty list instead of crashing.
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

    async def context_window(self, model: str) -> int | None:
        """Loaded context window in tokens, or ``None`` if unavailable."""
        if not model:
            return None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.management_url}/api/v0/models")
                resp.raise_for_status()
                for m in resp.json().get("data", []):
                    if m.get("id") == model:
                        loaded = m.get("loaded_context_length")
                        if loaded and loaded > 0:
                            return loaded
                        return m.get("max_context_length")
        except (httpx.HTTPError, KeyError, ValueError):
            return None
        return None

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
        """Non-streaming chat completion.

        Wrapped by :func:`with_retry` — transient connection errors,
        5xx, 429 (with Retry-After), and empty-content responses retry
        with exponential backoff. The circuit breaker on ``self._health``
        opens after 3 consecutive final failures.
        """
        body: dict = {"model": model, "messages": messages, "stream": False}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    @with_retry()
    async def chat_with_tools(
        self,
        messages: list[dict],
        *,
        tools: list[dict],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
        timeout: float = 120.0,
    ) -> dict:
        """Non-streaming chat completion with OpenAI-shaped tool calls.

        Sends ``tools`` (OpenAI function-definition shape) and
        ``tool_choice`` ("auto" by default; "required" forces a tool
        call on models that honor it). Returns
        ``{"content": str | None, "tool_calls": list[dict]}`` —
        ``content`` may be ``None`` when the model invoked tools, and
        ``tool_calls`` is a possibly-empty list of
        ``{id, type: "function", function: {name, arguments}}`` dicts.

        Wrapped by :func:`with_retry` with the same semantics as
        :meth:`chat` — transient connection errors, 5xx, and 429 retry
        with exponential backoff. Empty-content responses are NOT
        treated as failures here (the dict return bypasses the
        string-only empty-content check), since a tool-call response
        legitimately has ``content = None``.
        """
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            return {
                "content": msg.get("content"),
                "tool_calls": msg.get("tool_calls") or [],
            }

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

        ``idle_timeout`` is the per-chunk read timeout. **Do not change
        the default** — it is the load-bearing protection against LM Link
        silently stalling a slow remote-GPU stream.
        """
        body: dict = {"model": model, "messages": messages, "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature

        # connect=15s, read=idle_timeout (per-chunk), pool=timeout (overall)
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
