"""Stack template ABC — contract for the Architect's fast-path plugins.

Subclasses register via the ``development.stack_templates`` entry-point
group. When a :class:`BuildRequest`'s ``stack_hint`` matches a registered
template (per :meth:`StackTemplate.matches`), the Architect skips its LLM
call and uses :meth:`StackTemplate.build_plan` directly. This shaves the
~30s round-trip off builds whose stack is well-known.

Mirrors the ChatProvider / transform plugin patterns in prompt-enhancer:
discovery is via ``importlib.metadata.entry_points``, with the dispatch
key being the entry-point name (which should also be the class's ``name``
attribute for sanity).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from ..types import BuildRequest


class StackTemplate(ABC):
    """ABC for stack templates.

    Subclasses register via the ``development.stack_templates``
    entry-point group. Pure-Python — no I/O at module load time.
    """

    name: ClassVar[str]

    @abstractmethod
    def matches(self, stack_hint: str) -> bool:
        """Return True if this template handles ``stack_hint``.

        ``stack_hint`` is already lowercased by the caller. Implementations
        typically substring-match for stack keywords (e.g. ``"fastapi"`` and
        ``"sqlite"``).
        """

    @abstractmethod
    def build_plan(self, request: BuildRequest) -> dict[str, Any]:
        """Return the Architect's plan dict.

        Must produce the same shape the LLM path produces: ``stack``,
        ``layers``, ``dependencies``, ``constraints_satisfied``.
        Downstream stages don't know the plan came from a template.
        """
