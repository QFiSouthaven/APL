"""Coder — stage 2 (STUB).

Writes per-layer source files based on the Architect's plan. Will
delegate to ``development.layers.*`` generators (frontend, backend,
database, …) once those exist.

Tracking issue: https://github.com/QFiSouthaven/APL/issues — filed under
the "development v2.x" milestone.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .base import Stage


class CoderStage(Stage):
    name: ClassVar[str] = "coder"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "CoderStage is a v2.x feature — see "
            "https://github.com/QFiSouthaven/APL/issues for the roadmap. "
            "v0.1 ships only the Architect stage end-to-end."
        )
