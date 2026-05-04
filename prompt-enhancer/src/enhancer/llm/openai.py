"""OpenAI provider — official Chat Completions API at ``api.openai.com``.

Implementation uses ``httpx`` directly rather than the ``openai`` SDK to
keep the default install lean. Users who want SDK ergonomics can install
the ``[openai]`` extra (the SDK is declared there for completeness, but
this module does not import it).

Configuration (env vars, all read on every call so changes apply
without restart):

* ``OPENAI_API_KEY`` — required; module raises if missing.
* ``ENHANCER_OPENAI_BASE_URL`` — default ``https://api.openai.com/v1``;
  set to point at OpenAI-compat proxies (Azure, vLLM, etc.).
* ``ENHANCER_OPENAI_ORG`` — optional; sets ``OpenAI-Organization`` header.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator

import httpx

from .base import ChatProvider
from .resilience import ProviderHealth, with_retry, with_stream_retry

logger = logging.getLogger("enhancer.llm.openai")


class OpenAIAuthError(RuntimeError):
    """Raised when OPENAI_API_KEY is not set."""


class OpenAIProvider(ChatProvider):
    """OpenAI Chat Completions backend."""

    name = "openai"

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        default_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout = default_timeout
        self._health = ProviderHealth()

    # ── auth ────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise OpenAIAuthError(
                "OPENAI_API_KEY is not set. Export the env var or use the "
                "LM Studio provider for local inference."
            )
        h = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        org = os.environ.get("ENHANCER_OPENAI_ORG", "").strip()
        if org:
            h["OpenAI-Organization"] = org
        return h

    def _resolved_base_url(self) -> str:
        return os.environ.get("ENHANCER_OPENAI_BASE_URL", self.base_url).rstrip("/")

    # ── discovery ───────────────────────────────────────────────────

    async def list_models(self) -> list[str]:
        """List models available to the API key. Errors swallowed."""
        try:
            headers = self._headers()
        except OpenAIAuthError as exc:
            logger.warning("list_models: %s", exc)
            return []
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                resp = await client.get(f"{self._resolved_base_url()}/models")
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
        async with httpx.AsyncClient(timeout=timeout, headers=self._headers()) as client:
            resp = await client.post(
                f"{self._resolved_base_url()}/chat/completions", json=body,
            )
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
        """Streaming chat completion via SSE."""
        body: dict = {"model": model, "messages": messages, "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature

        stream_timeout = httpx.Timeout(timeout, connect=15.0, read=idle_timeout)

        async with httpx.AsyncClient(
            timeout=stream_timeout, headers=self._headers(),
        ) as client:
            async with client.stream(
                "POST", f"{self._resolved_base_url()}/chat/completions", json=body,
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
