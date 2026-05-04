"""Packager — stage 5 (STUB).

Packages the build's artifacts into a distributable form: zip, Docker
image, PyInstaller bundle, etc. Output target depends on the plan's
``stack.deployment``.

Tracking issue: https://github.com/QFiSouthaven/APL/issues — filed under
the "development v2.x" milestone.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .base import Stage


class PackagerStage(Stage):
    name: ClassVar[str] = "packager"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "PackagerStage is a v2.x feature — see "
            "https://github.com/QFiSouthaven/APL/issues for the roadmap. "
            "v0.1 ships only the Architect stage end-to-end."
        )
