"""Stage ABC — the contract every pipeline step implements.

The orchestrator drives stages by calling :meth:`run` with a shared
``ctx`` dict. The dict is the cross-stage scratch space:

* ``build_request``  — the original :class:`BuildRequest`.
* ``plan``           — the Architect's structured plan (added by stage 1).
* ``artifacts``      — dict of relative-path → file-contents.
* ``message_board``  — the live :class:`MessageBoard` for emitting events.

Stages return the (possibly extended) ``ctx`` so the orchestrator can
detect what each stage contributed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from ..llm_client import LLMClient

if TYPE_CHECKING:
    from ..reasoning_panel import ReasoningPanel


async def _chat_or_panel(
    llm: LLMClient,
    panel: "ReasoningPanel | None",
    messages: list[dict[str, Any]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    mode: str = "parallel",
    aggregator: str = "primary-wins",
) -> tuple[str, dict[str, Any] | None]:
    """Stage helper: route a chat call through a panel when wired.

    Returns ``(text, panel_telemetry)``. ``panel_telemetry`` is ``None``
    on the no-panel path (preserving v2.0 ctx shape) and a
    ``{"primary": ..., "partners": [{"name", "content", "ms", "error"}]}``
    dict when the panel was consulted. The aggregated text is what
    callers parse; per-slot raw outputs surface for observability.

    Mirrors the byte-for-byte semantics of the canonical helper in
    :class:`development.stages.reviewer.ReviewerStage._chat_or_panel`
    so every stage that opts into the panel surfaces telemetry in the
    same shape.
    """
    if panel is None:
        text = await llm.chat(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        return text, None

    result = await panel.consult(
        messages,
        mode=mode,
        aggregator=aggregator,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    telemetry = {
        "primary": result.primary.content,
        "partners": [
            {
                "name": p.slot_name,
                "content": p.content,
                "ms": p.duration_ms,
                "error": p.error,
            }
            for p in result.partners
        ],
    }
    return result.aggregated, telemetry


class Stage(ABC):
    """Single step in the build pipeline.

    Subclasses set the class-level ``name`` attribute (used in events,
    logs, and ``stages_completed``) and implement :meth:`run`.

    v2.1+ adds an optional ``reasoning_panel`` parameter so subclasses
    can opt into multi-LLM reasoning (parallel critique, sequential
    deliberation, consensus voting) without reshaping the existing
    single-LLM code path. The default is ``None`` — backward-compat is
    load-bearing: every v2.0 stage subclass continues to work
    unchanged when no panel is supplied. Subclasses that DO opt in
    branch on ``self._reasoning_panel is None`` and only consult the
    panel when a real one is wired. The base class itself never uses
    the panel — that's a subclass concern.
    """

    name: ClassVar[str]

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        reasoning_panel: "ReasoningPanel | None" = None,
    ) -> None:
        self._llm = llm_client
        self._reasoning_panel = reasoning_panel

    @abstractmethod
    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Mutate/return ``ctx`` with this stage's contributions.

        Must be idempotent within a single build — the orchestrator
        does not retry on its own, but a higher-level resume layer
        (v2.x) may replay completed stages from the message board.
        """
        raise NotImplementedError
