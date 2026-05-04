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
from typing import Any, ClassVar

from ..llm_client import LLMClient


class Stage(ABC):
    """Single step in the build pipeline.

    Subclasses set the class-level ``name`` attribute (used in events,
    logs, and ``stages_completed``) and implement :meth:`run`.
    """

    name: ClassVar[str]

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    @abstractmethod
    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Mutate/return ``ctx`` with this stage's contributions.

        Must be idempotent within a single build — the orchestrator
        does not retry on its own, but a higher-level resume layer
        (v2.x) may replay completed stages from the message board.
        """
        raise NotImplementedError
