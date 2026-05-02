"""OpenAI provider — stub for v1.1.

Install with ``pip install prompt-enhancer[openai]`` (pulls the official
``openai`` SDK). Implementation will use the streaming chat-completions
API and a sensible idle_timeout via ``timeout=httpx.Timeout(...)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .base import ChatProvider


_INSTALL_HINT = (
    "OpenAI provider is not implemented in v1.0. "
    "Install with `pip install prompt-enhancer[openai]` once v1.1 ships. "
    "Until then, use the LM Studio provider."
)


class OpenAIProvider(ChatProvider):
    name = "openai"

    async def list_models(self) -> list[str]:
        raise NotImplementedError(_INSTALL_HINT)

    async def chat(self, messages, *, model, temperature=None, max_tokens=None,
                   timeout=120.0) -> str:
        raise NotImplementedError(_INSTALL_HINT)

    async def chat_stream(self, messages, *, model, temperature=None, max_tokens=None,
                          timeout=600.0, idle_timeout=120.0) -> AsyncIterator[str]:
        raise NotImplementedError(_INSTALL_HINT)
        yield ""  # pragma: no cover
