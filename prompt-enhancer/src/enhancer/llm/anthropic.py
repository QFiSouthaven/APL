"""Anthropic provider — stub for v1.1.

Install with ``pip install prompt-enhancer[anthropic]`` (pulls the
official ``anthropic`` SDK). Anthropic's Messages API maps cleanly onto
:class:`ChatProvider`; the only translation needed is system message
handling (Anthropic carries ``system`` as a top-level field, not a
message role).

LM Studio's Anthropic-compatible endpoint at ``/v1/messages`` may be a
faster path to ship — see ``swarm-agent-dev/CLAUDE.md`` "LM Studio —
Anthropic Messages API" notes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .base import ChatProvider


_INSTALL_HINT = (
    "Anthropic provider is not implemented in v1.0. "
    "Install with `pip install prompt-enhancer[anthropic]` once v1.1 ships. "
    "Until then, use the LM Studio provider (which can also expose "
    "Anthropic-compatible endpoints)."
)


class AnthropicProvider(ChatProvider):
    name = "anthropic"

    async def list_models(self) -> list[str]:
        raise NotImplementedError(_INSTALL_HINT)

    async def chat(self, messages, *, model, temperature=None, max_tokens=None,
                   timeout=120.0) -> str:
        raise NotImplementedError(_INSTALL_HINT)

    async def chat_stream(self, messages, *, model, temperature=None, max_tokens=None,
                          timeout=600.0, idle_timeout=120.0) -> AsyncIterator[str]:
        raise NotImplementedError(_INSTALL_HINT)
        yield ""  # pragma: no cover
