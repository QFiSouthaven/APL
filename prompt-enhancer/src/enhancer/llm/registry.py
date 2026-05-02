"""Provider registry — discover by name, load by config.

Future-proofing: third-party providers can register via the
``enhancer.providers`` entry-point group in their own ``pyproject.toml``::

    [project.entry-points."enhancer.providers"]
    myllm = "my_pkg.provider:MyLLMProvider"

For now we only ship LM Studio with stubs for Ollama / OpenAI /
Anthropic. ``get_provider(name)`` raises ``NotImplementedError`` with an
install hint when a stubbed backend is requested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ChatProvider

if TYPE_CHECKING:
    from ..config import Settings


def get_provider(settings: Settings) -> ChatProvider:
    """Return the ChatProvider implied by ``settings.provider``."""
    name = settings.provider.lower().strip()
    if name == "lmstudio":
        from .lmstudio import LMStudioProvider
        return LMStudioProvider(
            base_url=settings.lms_base_url,
            management_url=settings.lms_management_url,
            default_timeout=settings.request_timeout,
        )
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider()
    if name == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider()
    if name == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider()
    raise ValueError(
        f"Unknown provider: {settings.provider!r}. "
        f"Supported: lmstudio, ollama, openai, anthropic."
    )
