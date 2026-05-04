"""Orchestrator — wires a BuildRequest through the staged pipeline.

The architecture diagram has the orchestrator on the left, fanning
events to a message board, with the actual code-generating LLM and
reviewer LLM on the right. This module is the left-side box.

v0.1 only runs the Architect stage. The class is built to take a list
of stages so the upgrade path is "pass more stages in" without
refactoring.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

from .llm_client import LLMClient
from .messageboard import MessageBoard
from .stages import ArchitectStage, Stage
from .types import (
    BUILD_DONE,
    BUILD_FAILED,
    BUILD_STARTED,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_STARTED,
    BuildRequest,
    BuildResult,
)

logger = logging.getLogger("development.orchestrator")


class Orchestrator:
    """Drives a BuildRequest through a Sequence[Stage]."""

    def __init__(
        self,
        llm_client: LLMClient,
        message_board: MessageBoard,
        *,
        stages: Sequence[Stage] | None = None,
    ) -> None:
        self._llm = llm_client
        self._board = message_board
        # Default pipeline: just the Architect for v0.1. Tests / future
        # versions can pass an explicit list to extend.
        self._stages: list[Stage] = list(stages) if stages else [ArchitectStage(llm_client)]

    @property
    def stages(self) -> list[Stage]:
        """Read-only view of the configured pipeline (for diagnostics)."""
        return list(self._stages)

    async def build(self, request: BuildRequest) -> BuildResult:
        """Run the full pipeline for a single BuildRequest.

        Publishes BUILD_STARTED / STAGE_* / BUILD_DONE events to the
        message board so subscribers can render progress. A failing
        stage publishes STAGE_FAILED + BUILD_FAILED, but still returns
        a partial BuildResult with whatever earlier stages completed.
        """
        started_ns = time.perf_counter_ns()
        self._board.publish(BUILD_STARTED, {"request": request.to_dict()})

        ctx: dict = {
            "build_request": request,
            "plan": {},
            "artifacts": {},
            "message_board": self._board,
        }
        completed: list[str] = []
        errors: list[str] = []

        for stage in self._stages:
            self._board.publish(STAGE_STARTED, {"stage": stage.name})
            try:
                ctx = await stage.run(ctx)
            except Exception as exc:  # noqa: BLE001 — record + halt
                msg = f"{stage.name}: {type(exc).__name__}: {exc}"
                logger.exception("Stage %s failed", stage.name)
                errors.append(msg)
                self._board.publish(
                    STAGE_FAILED,
                    {"stage": stage.name, "error": msg},
                )
                self._board.publish(
                    BUILD_FAILED,
                    {"stage": stage.name, "error": msg},
                )
                break
            completed.append(stage.name)
            self._board.publish(
                STAGE_DONE,
                {
                    "stage": stage.name,
                    # Surface a thin summary; full plan is too noisy.
                    "summary": _summarize_ctx(ctx),
                },
            )

        duration_ms = (time.perf_counter_ns() - started_ns) // 1_000_000

        result = BuildResult(
            request=request,
            stages_completed=tuple(completed),
            artifacts=dict(ctx.get("artifacts", {})),
            plan=dict(ctx.get("plan", {})),
            duration_ms=int(duration_ms),
            errors=tuple(errors),
        )

        if not errors:
            self._board.publish(
                BUILD_DONE,
                {"result": result.to_dict()},
            )
        return result


def _summarize_ctx(ctx: dict) -> dict:
    """Tiny ctx digest for STAGE_DONE events.

    The full plan can be many KB; replaying it on every message-board
    event would be wasteful. This returns just shape info.
    """
    plan = ctx.get("plan") or {}
    return {
        "plan_layers": len(plan.get("layers") or []),
        "plan_deps": len(plan.get("dependencies") or []),
        "artifact_count": len(ctx.get("artifacts") or {}),
    }
