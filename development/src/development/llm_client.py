"""Wrap prompt-enhancer's LMStudioProvider for the development service.

We share one ``ChatProvider`` implementation across products by
path-injecting the prompt-enhancer ``src/`` directory at import time.
Long-term (v2.x) the provider abstraction will be extracted into a
standalone ``apl-llm`` package living under ``APL/lab/``; for now the
path-injection keeps the umbrella honest about there being a single
LM Studio integration without forcing a redundant pip install.

Usage:

    from development.llm_client import LLMClient
    client = LLMClient()  # picks settings.provider_base_url
    text = await client.chat([{"role": "user", "content": "hi"}])
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Protocol

from .config import SETTINGS

logger = logging.getLogger("development.llm_client")


# Find APL/prompt-enhancer/src and prepend to sys.path so we can import
# ``enhancer.llm.lmstudio.LMStudioProvider`` without a pip install.
#
# This file is at: APL/development/src/development/llm_client.py
#                  parents[0] = development/
#                  parents[1] = src/
#                  parents[2] = development/   (the project root)
#                  parents[3] = APL/
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PE_SRC = _REPO_ROOT / "prompt-enhancer" / "src"
if _PE_SRC.exists() and str(_PE_SRC) not in sys.path:
    sys.path.insert(0, str(_PE_SRC))


# Late import — we only attempt this after the path is patched.
try:
    from enhancer.llm.lmstudio import LMStudioProvider  # type: ignore[import-not-found]

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover — exercised only when sibling missing
    LMStudioProvider = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR = exc
    logger.warning(
        "Could not import LMStudioProvider from sibling prompt-enhancer at %s: %s. "
        "LLMClient will only work if a real provider is injected.",
        _PE_SRC,
        exc,
    )


class _ChatProtocol(Protocol):
    """Minimal surface the orchestrator/stages need from a provider.

    Tests substitute a fake conforming to this Protocol; production
    uses the LMStudioProvider import above. We don't subclass
    ChatProvider directly because the prompt-enhancer import may fail
    in some environments and we don't want hard module-load coupling.
    """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        timeout: float = ...,
    ) -> str: ...


class LLMClient:
    """Stage-facing wrapper around a chat provider.

    Stages call :meth:`chat` with a list of OpenAI-style messages; the
    client picks up the configured base URL + default model from
    :data:`development.config.SETTINGS` so callers don't need to thread
    those through.
    """

    def __init__(
        self,
        provider: _ChatProtocol | None = None,
        *,
        default_model: str | None = None,
    ) -> None:
        if provider is None:
            if LMStudioProvider is None:
                raise RuntimeError(
                    "LMStudioProvider is unavailable. Install prompt-enhancer's "
                    "package or pass a provider explicitly. Original import "
                    f"error: {_IMPORT_ERROR!r}"
                )
            provider = LMStudioProvider(base_url=SETTINGS.provider_base_url)
        self._provider = provider
        self._default_model = default_model or SETTINGS.default_model

    @property
    def model(self) -> str:
        return self._default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> str:
        """Forward a chat to the underlying provider.

        Tightly mirrors the ChatProvider.chat surface so swapping in a
        different backend (Ollama, OpenAI) requires zero changes here.
        """
        return await self._provider.chat(
            messages,
            model=model or self._default_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Tool-aware chat returning ``{content, tool_calls}``.

        OpenAI-shaped output: ``{"content": str | None, "tool_calls": list[dict]}``.
        Each tool_call entry has the shape::

            {"id": str, "name": str, "arguments": dict}

        Implementation strategy: prompt-enhancer's :class:`LMStudioProvider`
        does not currently expose a tools-aware method, so we use a
        documented MESSAGES-FALLBACK: we synthesize a system-message
        addendum describing the tool catalog and ask the model to emit
        a JSON-RPC-style ``tool_calls`` list when it wants to call one.
        We then parse the response.

        If the underlying provider grows a native ``chat_with_tools`` we
        prefer it (this method delegates first); otherwise we fall back.
        """
        # 1) Native path: if the provider exposes chat_with_tools, use it.
        native = getattr(self._provider, "chat_with_tools", None)
        if callable(native):
            return await native(  # type: ignore[no-any-return]
                messages,
                tools=tools,
                model=model or self._default_model,
                temperature=temperature,
                timeout=timeout,
            )

        # 2) Messages-fallback path. Inject a system-message addendum
        # describing the catalog and asking for a JSON tool_calls payload.
        addendum = _build_tool_use_system_addendum(tools)
        augmented = [{"role": "system", "content": addendum}, *messages]
        raw = await self._provider.chat(
            augmented,
            model=model or self._default_model,
            temperature=temperature,
            max_tokens=None,
            timeout=timeout,
        )
        return _parse_tool_use_response(raw)


# ── messages-fallback helpers (module-private) ──────────────────────────


def _build_tool_use_system_addendum(tools: list[dict[str, Any]]) -> str:
    """Render a system message describing the tool catalog + emit-format.

    The instruction asks the model to either:

      * emit a JSON object ``{"tool_calls": [{"name": ..., "arguments": {...}}, ...]}``
        when it wants to call tools, OR
      * emit a JSON object ``{"content": "..."}`` when it has the final answer.

    Any well-formed JSON matching either shape is parsed by
    :func:`_parse_tool_use_response`.
    """
    import json as _json

    catalog_brief: list[dict[str, Any]] = []
    for entry in tools or []:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(fn, dict):
            continue
        catalog_brief.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description"),
                "parameters": fn.get("parameters"),
            }
        )
    return (
        "You may call tools to ground your final answer. The available "
        "tools are described below as JSON Schema. To call tools, output "
        'a JSON object: {"tool_calls": [{"name": "<tool>", "arguments": '
        "{<args>}}, ...]}. To return a final answer with no tool calls, "
        'output: {"content": "<your answer>"}. Output ONLY one JSON '
        "object, no prose, no fences.\n\nTools:\n"
        + _json.dumps(catalog_brief, indent=2)
    )


def _parse_tool_use_response(raw: str) -> dict[str, Any]:
    """Parse the messages-fallback response into the tool-call envelope.

    On parse failure we fall back to ``{"content": raw, "tool_calls": []}``
    so the caller still sees the raw text and can let the LLM finish.
    """
    from ._json_utils import parse_llm_json

    parsed = parse_llm_json(raw or "")
    if parsed is None:
        return {"content": raw, "tool_calls": []}

    tool_calls_in = parsed.get("tool_calls")
    out_calls: list[dict[str, Any]] = []
    if isinstance(tool_calls_in, list):
        for i, tc in enumerate(tool_calls_in):
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or (tc.get("function", {}) or {}).get("name")
            if not isinstance(name, str):
                continue
            args = tc.get("arguments")
            if isinstance(args, str):
                # OpenAI sometimes emits arguments as a JSON string.
                import json as _json
                try:
                    args = _json.loads(args)
                except (TypeError, ValueError):
                    args = {}
            if not isinstance(args, dict):
                args = {}
            out_calls.append(
                {"id": tc.get("id") or f"call_{i}", "name": name, "arguments": args}
            )

    content = parsed.get("content")
    if not isinstance(content, str):
        content = None if out_calls else (raw or "")
    return {"content": content, "tool_calls": out_calls}
