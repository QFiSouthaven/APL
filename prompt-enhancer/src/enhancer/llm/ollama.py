"""Ollama provider — stub for v1.1.

Wire this up against the Ollama HTTP API at ``localhost:11434``. The
Ollama OpenAI-compat endpoint at ``/v1/chat/completions`` makes the
implementation almost identical to :class:`LMStudioProvider`.

Track at https://github.com/QFiSouthaven/APL/issues for v1.1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .base import ChatProvider
from .resilience import ProviderHealth


_INSTALL_HINT = (
    "Ollama provider is not implemented in v1.0. "
    "Track v1.1 at https://github.com/QFiSouthaven/APL/issues. "
    "Until then, use the LM Studio provider or contribute the implementation."
)


class OllamaProvider(ChatProvider):
    name = "ollama"

    def __init__(self) -> None:
        # Parity with LMStudioProvider — once chat() is implemented the
        # @with_retry / @with_stream_retry decorators consult self._health.
        self._health = ProviderHealth()

    async def list_models(self) -> list[str]:
        raise NotImplementedError(_INSTALL_HINT)

    async def chat(self, messages, *, model, temperature=None, max_tokens=None,
                   timeout=120.0) -> str:
        raise NotImplementedError(_INSTALL_HINT)

    async def chat_stream(self, messages, *, model, temperature=None, max_tokens=None,
                          timeout=600.0, idle_timeout=120.0) -> AsyncIterator[str]:
        raise NotImplementedError(_INSTALL_HINT)
        yield ""  # pragma: no cover — make this a proper async generator
