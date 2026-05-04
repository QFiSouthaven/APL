"""Coder — stage 2: turn the Architect's plan into source files.

Walks ``ctx["plan"]["layers"]`` and dispatches each entry to the
matching generator from :data:`development.layers.LAYER_GENERATORS`.
Each generator returns a ``{path: content}`` dict; the Coder merges
them all into ``ctx["artifacts"]``.

Per-layer error policy:

* If the layer name has no registered generator (e.g. ``tests`` or
  ``docs``), the Coder publishes a ``STAGE_PROGRESS`` event with
  ``skipped=True`` and continues. This implements the "skip" cells of
  the Stage × Layer matrix (DEVELOPMENT_FRAMEWORK.md §4) — every
  (stage, layer) pair is visited even when no LLM call fires.
* If a generator raises :class:`LayerGenerationError` (the LLM never
  produces parseable JSON), the Coder lets the exception propagate so
  the Orchestrator can publish ``STAGE_FAILED``/``BUILD_FAILED``.

The Coder requires an Architect plan in ctx — it raises
``RuntimeError`` if ``ctx["plan"]`` is missing or empty.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from ..layers import LAYER_GENERATORS
from ..types import STAGE_PROGRESS
from .base import Stage

logger = logging.getLogger("development.stages.coder")


class CoderStage(Stage):
    """Stage 2: generate per-layer source files from the Architect's plan.

    Reads ``ctx["plan"]`` (must exist; raises ``RuntimeError`` otherwise),
    walks ``plan["layers"]``, and for each layer dispatches to the matching
    async generator from :data:`development.layers.LAYER_GENERATORS` (keyed
    by lowercased layer name). Each generator returns ``{path: content}``
    which is merged into ``ctx["artifacts"]``.

    Layers whose ``name`` does not appear in the registry (e.g. ``tests``,
    ``docs``) are skipped with a ``STAGE_PROGRESS`` event carrying
    ``skipped=True``; they are owned by later stages per the framework
    matrix. A layer whose generator raises ``LayerGenerationError``
    propagates the error so the Orchestrator can fail the build.
    """

    name: ClassVar[str] = "coder"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        plan = ctx.get("plan")
        if not plan:
            raise RuntimeError(
                "CoderStage requires ctx['plan'] populated by the Architect. "
                "Run ArchitectStage before CoderStage."
            )

        layers = plan.get("layers") or []
        # Two parallel views of generated files:
        # - artifacts: flat path -> content; populates BuildResult.artifacts.
        # - artifacts_by_layer: nested layer -> {path: content}; consumed by
        #   the Reviewer stage so it can critique each layer in isolation
        #   without re-deriving the layer/file mapping from the plan.
        artifacts: dict[str, str] = ctx.setdefault("artifacts", {})
        artifacts_by_layer: dict[str, dict[str, str]] = ctx.setdefault(
            "artifacts_by_layer", {}
        )
        board = ctx.get("message_board")

        for layer in layers:
            if not isinstance(layer, dict):
                # Defensive: malformed plan entry. Skip it loudly.
                logger.warning("CoderStage: skipping non-dict layer entry: %r", layer)
                continue

            layer_name = str(layer.get("name", "")).strip()
            key = layer_name.lower()
            generator = LAYER_GENERATORS.get(key)

            if generator is None:
                # Unhandled layer — skip per the matrix's "skip" cells.
                logger.info("CoderStage: no generator for layer %r; skipping.", layer_name)
                if board is not None:
                    board.publish(
                        STAGE_PROGRESS,
                        {
                            "stage": self.name,
                            "layer": layer_name,
                            "skipped": True,
                            "reason": "no_generator",
                        },
                    )
                continue

            files = await generator(plan, layer, self._llm)
            artifacts.update(files)
            # Preserve the layer mapping for the Reviewer.
            artifacts_by_layer[layer_name] = dict(files)

            if board is not None:
                board.publish(
                    STAGE_PROGRESS,
                    {
                        "stage": self.name,
                        "layer": layer_name,
                        "files_generated": len(files),
                    },
                )

        ctx["artifacts"] = artifacts
        ctx["artifacts_by_layer"] = artifacts_by_layer
        return ctx
