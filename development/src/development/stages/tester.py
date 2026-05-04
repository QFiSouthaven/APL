"""Tester — stage 4 (STUB).

Generates and runs a test suite over the produced artifacts. v2.x
will use a sandboxed worker (likely the round-robin Charlie workspace
pattern) to actually execute tests instead of just generating them.

Tracking issue: https://github.com/QFiSouthaven/APL/issues — filed under
the "development v2.x" milestone.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .base import Stage


class TesterStage(Stage):
    name: ClassVar[str] = "tester"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "TesterStage is a v2.x feature — see "
            "https://github.com/QFiSouthaven/APL/issues for the roadmap. "
            "v0.1 ships only the Architect stage end-to-end."
        )
