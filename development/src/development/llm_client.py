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
