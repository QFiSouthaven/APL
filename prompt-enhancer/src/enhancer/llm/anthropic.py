"""Anthropic provider — Messages API at ``api.anthropic.com``.

Targets Anthropic's native ``/v1/messages`` shape, which means this
provider also works against LM Studio's Anthropic-compatible endpoint
(set ``ENHANCER_ANTHROPIC_BASE_URL=http://localhost:1234`` to use the
local rig instead of the hosted API).

Two key shape differences from OpenAI's chat-completions:

* ``system`` is a top-level field, NOT a message role. We extract the
  first ``role=system`` message and lift it.
* SSE events are typed: ``event: content_block_delta`` with
  ``data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}``
  — we parse only the data lines and switch on the JSON ``type`` field.
* ``max_tokens`` is REQUIRED by the API; we default to 4096 when the
  caller didn't pass one.

Configuration (env vars):

* ``ANTHROPIC_API_KEY`` — required for the hosted API; optional for
  LM Studio compat path. Sent as ``x-api-key`` header (not Bearer).
* ``ENHANCER_ANTHROPIC_BASE_URL`` — default ``https://api.anthropic.com``.
* ``ENHANCER_ANTHROPIC_VERSION`` — default ``2023-06-01``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator

import httpx

from .base import ChatProvider
from .resilience import ProviderHealth, with_retry, with_stream_retry

logger = logging.getLogger("enhancer.llm.anthropic")

# Anthropic's API requires max_tokens; pick a sane default that works
# for all current Claude models without forcing users to think about it.
DEFAULT_MAX_TOKENS = 4096


class AnthropicAuthError(RuntimeError):
    """Raised when ANTHROPIC_API_KEY is not set AND the base URL is the
    hosted endpoint (LM Studio compat path tolerates a missing key)."""


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Lift the first ``role=system`` message into the top-level field.

    Returns ``(system_text, remaining_messages)``. Multiple system
    messages are concatenated with double newlines (rare but possible).
    """
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, str) and content:
                system_parts.append(content)
        else:
            rest.append(m)
    return "\n\n".join(system_parts), rest


class AnthropicProvider(ChatProvider):
    """Anthropic Messages API backend."""

    name = "anthropic"

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        default_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout = default_timeout
        self._health = ProviderHealth()

    # ── auth + headers ──────────────────────────────────────────────

    def _resolved_base_url(self) -> str:
        return os.environ.get("ENHANCER_ANTHROPIC_BASE_URL", self.base_url).rstrip("/")

    def _is_hosted(self) -> bool:
        """True when targeting the real Anthropic API; False for LM Studio compat."""
        return "anthropic.com" in self._resolved_base_url()

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key and self._is_hosted():
            raise AnthropicAuthError(
                "ANTHROPIC_API_KEY is not set. Export the env var, or set "
                "ENHANCER_ANTHROPIC_BASE_URL to LM Studio's host for the "
                "compat path."
            )
        h = {
            "Content-Type": "application/json",
            "anthropic-version": os.environ.get(
                "ENHANCER_ANTHROPIC_VERSION", "2023-06-01",
            ),
        }
        if api_key:
            h["x-api-key"] = api_key
        return h

    # ── request body ────────────────────────────────────────────────

    def _build_body(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict:
        system_text, rest = _split_system(messages)
        body: dict = {
            "model": model,
            "messages": rest,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "stream": stream,
        }
        if system_text:
            body["system"] = system_text
        if temperature is not None:
            body["temperature"] = temperature
        return body

    # ── discovery ───────────────────────────────────────────────────

    async def list_models(self) -> list[str]:
        """List Claude models available to the API key.

        Anthropic's ``/v1/models`` returns a list under ``data``. Errors
        swallowed; LM Studio compat may not implement this endpoint.
        """
        try:
            headers = self._headers()
        except AnthropicAuthError as exc:
            logger.warning("list_models: %s", exc)
            return []
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                resp = await client.get(f"{self._resolved_base_url()}/v1/models")
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
        """Non-streaming Messages API call.

        Returns the concatenated text from all ``content[].text`` blocks.
        """
        body = self._build_body(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, stream=False,
        )
        async with httpx.AsyncClient(timeout=timeout, headers=self._headers()) as client:
            resp = await client.post(
                f"{self._resolved_base_url()}/v1/messages", json=body,
            )
            resp.raise_for_status()
            payload = resp.json()
            blocks = payload.get("content", [])
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            return "".join(texts)

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
        """Streaming Messages API call.

        Parses Anthropic's typed SSE: yields the ``delta.text`` from
        every ``content_block_delta`` event whose delta is a
        ``text_delta``. Other event types (message_start, ping, usage
        deltas) are ignored.
        """
        body = self._build_body(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, stream=True,
        )
        stream_timeout = httpx.Timeout(timeout, connect=15.0, read=idle_timeout)

        async with httpx.AsyncClient(
            timeout=stream_timeout, headers=self._headers(),
        ) as client:
            async with client.stream(
                "POST", f"{self._resolved_base_url()}/v1/messages", json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("type") != "content_block_delta":
                        continue
                    delta = chunk.get("delta", {})
                    if delta.get("type") != "text_delta":
                        continue
                    text = delta.get("text", "")
                    if text:
                        yield text
