"""ChatProvider — the LLM abstraction.

Every backend (LM Studio, Ollama, OpenAI, Anthropic, …) implements this
ABC. The pipeline never imports a concrete provider; it always goes
through a ``ChatProvider`` instance. This is what unlocks multi-backend
support without rewriting the pipeline.

**Critical invariants** every implementation must preserve:

* :meth:`chat_stream` accepts an ``idle_timeout`` parameter (default
  ``120.0``); silent stalls within that window must raise
  ``httpx.ReadTimeout`` (or equivalent) so the pipeline can fail fast
  instead of hanging on a dead LM Link socket.
* Both :meth:`chat` and :meth:`chat_stream` accept a ``temperature`` and
  ``max_tokens`` kwarg threading through to the backend; **the pipeline
  passes user-controlled values to every call**.
* :meth:`list_models` returns a freshly-queried list (not cached) so the
  UI's model dropdown reflects whatever LM Studio currently has loaded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar


class ChatProvider(ABC):
    """Async chat API surface every LLM backend exposes."""

    name: ClassVar[str] = "abstract"

    # ── discovery ───────────────────────────────────────────────────

    @abstractmethod
    async def list_models(self) -> list[str]:
        """List currently-available model identifiers (not cached)."""

    async def context_window(self, model: str) -> int | None:
        """Optional: return the loaded context window in tokens.

        Default ``None`` — pipeline falls back to model-name regex /
        param-count heuristics in ``core.budgeting.detect_context_budget``.
        """
        return None

    # ── completion ──────────────────────────────────────────────────

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> str:
        """Non-streaming completion. Returns the full assistant content string.

        Implementations must apply ``temperature`` and ``max_tokens`` if not
        ``None``. ``timeout`` is the overall request timeout.
        """

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        idle_timeout: float = 120.0,
    ) -> AsyncIterator[str]:
        """Streaming completion — yields incremental token strings.

        ``idle_timeout`` is the per-chunk read timeout. **It is critical
        that this value is honored**: when LM Link silently stalls a slow
        remote-GPU stream, only the per-chunk timeout fires; the overall
        ``timeout`` may be hours long.

        Note: this method is declared as a regular function (not ``async
        def``) because async generators in ABCs can't be marked with
        ``@abstractmethod`` cleanly across mypy versions. Implementations
        define it as ``async def`` and ``yield`` tokens normally.
        """
