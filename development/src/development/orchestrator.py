"""Orchestrator — wires a BuildRequest through the staged pipeline.

The architecture diagram has the orchestrator on the left, fanning
events to a message board, with the actual code-generating LLM and
reviewer LLM on the right. This module is the left-side box.

Pipeline-shape design decision (v0.5): the default ``Orchestrator()``
pipeline is **Architect → Coder → Reviewer → Tester → Packager** — the
full five-stage chain that turns a goal into critiqued, tested, and
deployable artifacts. The previous defaults (Architect-only / Architect
→ Coder / Architect → Coder → Reviewer / Architect → Coder → Reviewer
→ Tester) are still reachable via the ``stages=`` override;
``include_coder=True`` is preserved as a no-op flag to keep v0.2
callsites compiling.

v2.1: optional ``reasoning_panel``. When supplied to
``Orchestrator.__init__``, the panel is threaded into every stage that
opts in. **In v2.1 only the Reviewer wires the panel** — Architect,
Coder, Tester, and Packager accept the kwarg (because ``Stage.__init__``
declares it) but currently ignore it. v2.2 will extend wiring to the
other stages once their panel-aware critique modes are designed. The
panel's mode + aggregator come from ``BuildRequest.panel_mode`` /
``panel_aggregator`` per-build, defaulting to ``parallel`` +
``primary-wins``.

Stage order is load-bearing per the framework doc:
- Architect produces ``ctx["plan"]``
- Coder consumes plan, produces ``ctx["artifacts"]`` (flat) AND
  ``ctx["artifacts_by_layer"]`` (nested)
- Reviewer consumes ``ctx["artifacts_by_layer"]``, may invoke layer
  generators for one bounded loopback per layer, re-syncs flat view
  at end so ``BuildResult.artifacts`` reflects critiqued state.
- Tester consumes ``ctx["artifacts_by_layer"]``, generates and runs a
  per-layer test suite, may invoke layer generators for one MORE
  bounded loopback per layer (separate budget from the Reviewer's),
  re-syncs flat view if it regenerated. Records results in
  ``ctx["test_results"]``.
- Packager reads the plan + final artifacts and emits Dockerfile,
  docker-compose.yml, .env.example, deploy.sh, deploy.ps1, README.md
  under a synthetic "packaging" layer. Validates each file
  structurally and records the verdicts in
  ``ctx["package_validation"]``. Validation failures are warnings,
  not gates — the build always completes once Packager runs.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Sequence

from .llm_client import LLMClient
from .messageboard import MessageBoard
from .reviewers import REVIEWERS, get_reviewer
from .stages import (
    ArchitectStage,
    CoderStage,
    PackagerStage,
    ReviewerStage,
    Stage,
    TesterStage,
)
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

if TYPE_CHECKING:
    from .reasoning_panel import ReasoningPanel

logger = logging.getLogger("development.orchestrator")


class Orchestrator:
    """Drives a BuildRequest through a Sequence[Stage]."""

    def __init__(
        self,
        llm_client: LLMClient,
        message_board: MessageBoard,
        *,
        stages: Sequence[Stage] | None = None,
        include_coder: bool = False,
        reasoning_panel: "ReasoningPanel | None" = None,
    ) -> None:
        self._llm = llm_client
        self._board = message_board
        self._reasoning_panel = reasoning_panel
        # Default pipeline (v0.5): Architect → Coder → Reviewer → Tester
        # → Packager. The ``include_coder`` flag is a no-op kept for
        # source compat with v0.2 callsites; pass ``stages=[ArchitectStage(...)]``
        # to restore the v0.1 single-stage shape if needed.
        #
        # v2.1: ``reasoning_panel`` is threaded into every stage that
        # accepts it. Currently only the Reviewer USES the panel — the
        # others (Architect/Coder/Tester/Packager) accept the kwarg via
        # ``Stage.__init__`` but ignore it pending v2.2 wiring.
        if stages is not None:
            self._stages: list[Stage] = list(stages)
        else:
            self._stages = [
                ArchitectStage(llm_client),
                CoderStage(llm_client),
                ReviewerStage(llm_client, reasoning_panel=reasoning_panel),
                TesterStage(llm_client),
                PackagerStage(llm_client),
            ]

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

        v2.0: ``request.reviewer`` selects the Stage-3 implementation
        per-build. We materialize a build-specific stages list rather
        than mutating ``self._stages`` so concurrent builds with
        different reviewers don't trample each other's pipelines.
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

        # Per-build reviewer dispatch. We don't mutate self._stages
        # because (a) concurrent builds may pick different reviewers and
        # (b) the orchestrator's stages property is documented as a
        # read-only diagnostic view of the configured pipeline. Build
        # the substituted list once, iterate it, leave self._stages
        # untouched.
        stages_for_this_build: list[Stage] = []
        for stage in self._stages:
            if (
                isinstance(stage, ReviewerStage)
                and request.reviewer != "single-pass"
                and request.reviewer in REVIEWERS
            ):
                cls = get_reviewer(request.reviewer)
                # Thread the panel only into the canonical ReviewerStage
                # — alternate reviewers (RoundRobinReviewer) do their
                # own multi-LLM thing and don't yet accept a panel kwarg.
                if cls is ReviewerStage:
                    stages_for_this_build.append(
                        cls(self._llm, reasoning_panel=self._reasoning_panel)
                    )
                else:
                    stages_for_this_build.append(cls(self._llm))
            else:
                stages_for_this_build.append(stage)

        for stage in stages_for_this_build:
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
            test_results=dict(ctx.get("test_results", {})),
            package_validation=dict(ctx.get("package_validation", {})),
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
