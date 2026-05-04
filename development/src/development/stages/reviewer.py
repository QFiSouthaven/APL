"""Reviewer — stage 3 (STUB).

Static review of the Coder's output: flag obvious issues, suggest
fixes, mark the build as needing rework or ready for tests.

Tracking issue: https://github.com/QFiSouthaven/APL/issues — filed under
the "development v2.x" milestone.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .base import Stage


class ReviewerStage(Stage):
    name: ClassVar[str] = "reviewer"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "ReviewerStage is a v2.x feature — see "
            "https://github.com/QFiSouthaven/APL/issues for the roadmap. "
            "v0.1 ships only the Architect stage end-to-end."
        )
