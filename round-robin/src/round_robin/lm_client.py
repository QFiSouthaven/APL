"""LM Link client — single httpx.AsyncClient pointing at localhost:1234.

LM Studio's LM Link routes requests to remote machines internally based on the
requested `model` identifier, so all traffic from this app talks to localhost.

Notes on the two API surfaces this client uses:
  * /v1/*       — OpenAI-compatible (chat/completions, models)
  * /api/v0/*   — LM Studio native (richer per-model metadata, including
                  loaded_context_length, capabilities, state). Available since
                  LM Studio 0.3.6.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import (
    LMS_BASE_URL,
    LMS_TIMEOUT_CONNECT,
    LMS_TIMEOUT_READ,
    LMS_TIMEOUT_WRITE,
)

logger = logging.getLogger(__name__)


class LMLinkError(Exception):
    """Raised on LM Studio HTTP / parsing failures."""


def _native_base(openai_base: str) -> str:
    """Convert /v1 base URL to LM Studio's native /api/v0 base."""
    if openai_base.endswith("/v1"):
        return openai_base[:-3] + "/api/v0"
    return openai_base.rstrip("/") + "/api/v0"


def _extract_lms_error(response: httpx.Response) -> str:
    """LM Studio returns errors as `{"error": "<string>"}` — flat, NOT OpenAI shape.
    Pull the string out so we can surface a useful message instead of just the
    HTTP status code."""
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text or f"HTTP {response.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    return str(body)


class LMLinkClient:
    def __init__(self, base_url: str = LMS_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._native_base = _native_base(self.base_url)
        timeout = httpx.Timeout(
            connect=LMS_TIMEOUT_CONNECT,
            read=LMS_TIMEOUT_READ,
            write=LMS_TIMEOUT_WRITE,
            pool=LMS_TIMEOUT_CONNECT,
        )
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            limits=limits,
            http2=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def models(self) -> list[dict[str, Any]]:
        """Return raw model entries from /v1/models. LM Link adds remote ones."""
        try:
            r = await self._client.get(f"{self.base_url}/models", timeout=10.0)
            r.raise_for_status()
            payload = r.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise LMLinkError(f"Failed to list models: {exc}") from exc
        data = payload.get("data") or []
        return [m for m in data if isinstance(m, dict)]

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/models", timeout=3.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def model_info(self, model_id: str) -> dict[str, Any] | None:
        """Fetch rich per-model metadata from /api/v0/models/{id}.

        Returns None if the native API isn't available (e.g. older LM Studio)
        or the model isn't known. Never raises — callers fall back to defaults.

        Useful keys in the returned dict:
          state                  : "loaded" | "not-loaded"
          max_context_length     : architectural cap
          loaded_context_length  : actual configured window (only present when loaded)
          capabilities           : ["tool_use", ...]
          arch, quantization, type, publisher
        """
        try:
            r = await self._client.get(f"{self._native_base}/models/{model_id}", timeout=5.0)
            if r.status_code != 200:
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, json.JSONDecodeError):
            return None

    async def native_models(self) -> list[dict[str, Any]] | None:
        """List all known models with native metadata. None if /api/v0 unavailable."""
        try:
            r = await self._client.get(f"{self._native_base}/models", timeout=5.0)
            if r.status_code != 200:
                return None
            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            return [m for m in (data or []) if isinstance(m, dict)]
        except (httpx.HTTPError, json.JSONDecodeError):
            return None

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Non-streaming chat. Returns assistant text.

        Reasoning models (e.g. nvidia-nemotron-*-reasoning, deepseek-r1-distill-*)
        emit their final answer in `message.reasoning_content` rather than
        `message.content`. We prefer `content` and fall back to `reasoning_content`
        so callers get a usable string in either case.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        try:
            r = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LMLinkError(f"chat transport error for {model}: {exc}") from exc
        if r.status_code != 200:
            raise LMLinkError(
                f"chat failed for {model}: HTTP {r.status_code} — {_extract_lms_error(r)}"
            )
        try:
            data = r.json()
        except json.JSONDecodeError as exc:
            raise LMLinkError(f"chat returned non-JSON for {model}: {exc}") from exc
        try:
            choice = data["choices"][0]
            msg = choice.get("message", {}) or {}
            content = msg.get("content") or ""
            if not content.strip():
                # Reasoning-model fallback (nemotron, deepseek-r1-distill, etc.)
                content = msg.get("reasoning_content") or ""
            finish = choice.get("finish_reason")
            if finish == "length":
                logger.warning(
                    "chat for %s hit max_tokens; output may be truncated", model,
                )
            return content
        except (KeyError, IndexError, TypeError) as exc:
            raise LMLinkError(f"Malformed chat response: {data!r}") from exc

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Stream tokens (delta content strings) from /chat/completions SSE."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        if raw == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Skipping bad SSE chunk: %r", raw)
                        continue
                    try:
                        delta = chunk["choices"][0].get("delta", {})
                    except (KeyError, IndexError, TypeError):
                        continue
                    token = delta.get("content")
                    if token:
                        yield token
        except httpx.HTTPError as exc:
            raise LMLinkError(f"stream failed for {model}: {exc}") from exc
