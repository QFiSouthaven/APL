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

v2.0: opt-in ``tool_use=True`` mode. When enabled, instead of calling
the layer generator directly, we run a tool-loop where the LLM may
emit ``tool_calls`` against a small catalog (filesystem read, git
read-only, sandboxed shell exec). Default is ``tool_use=False`` — the
existing v0.5 behavior is preserved exactly.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from .._json_utils import parse_llm_json
from ..layers import LAYER_GENERATORS
from ..layers._common import build_user_prompt
from ..llm_client import LLMClient
from ..tools import (
    MAX_TOOL_CALLS_PER_LAYER,
    TOOL_CATALOG,
    dispatch_tool_call,
)
from ..types import STAGE_PROGRESS, LayerGenerationError
from .base import Stage, _chat_or_panel

logger = logging.getLogger("development.stages.coder")


# Layer-generation system prompt for the tool-use path. Mirrors the
# per-layer prompts (backend/frontend/...) but explicitly invites tool
# use. Final response shape is the same {path: content} dict so
# downstream merge logic doesn't change.
_TOOL_USE_SYSTEM_PROMPT = (
    "You are an engineer generating source files for one layer of a "
    "build plan. You have access to read-only filesystem and git tools "
    "plus a sandboxed shell-exec tool — use them to ground your output "
    "if helpful. When you have all the information you need, return a "
    "final JSON object whose keys are file paths and whose values are "
    "the file contents as strings. Output ONLY valid JSON, no prose, "
    "no markdown fences."
)


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

    v2.0: opt-in ``tool_use``. When ``True``, each layer runs through a
    tool-loop (LLM ⇄ TOOL_DISPATCH) instead of the one-shot generator;
    tool calls are bounded by ``tool_call_budget``.
    """

    name: ClassVar[str] = "coder"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        tool_use: bool = False,
        tool_call_budget: int | None = None,
        reasoning_panel: Any = None,
    ) -> None:
        super().__init__(llm_client, reasoning_panel=reasoning_panel)
        self._tool_use = bool(tool_use)
        # ``None`` → use the catalog's default; ``int`` → explicit override.
        self._tool_call_budget = (
            int(tool_call_budget)
            if tool_call_budget is not None
            else MAX_TOOL_CALLS_PER_LAYER
        )

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

        # Panel mode/aggregator come from the BuildRequest. v2.2: in
        # tool-use mode, the FIRST round per layer (the planning call,
        # before any tool loop iterations) routes through the panel
        # when one is wired. Subsequent tool-loop iterations stick to
        # the single-provider ``chat_with_tools`` path because the panel
        # has no native multi-provider tool-call protocol — partner
        # slots can't coherently emit tool_calls into a shared sandbox.
        # The aggregated planning text is appended as an assistant turn
        # so the tool loop sees the panel's reasoning before its first
        # tool decision. Per-stage telemetry (last layer's call) lands
        # in ``ctx["coder_panel"]``.
        request = ctx.get("build_request")
        panel_mode = getattr(request, "panel_mode", None) or "parallel"
        panel_aggregator = (
            getattr(request, "panel_aggregator", None) or "primary-wins"
        )
        last_panel_telemetry: dict[str, Any] | None = None

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

            tool_calls_used = 0
            if self._tool_use:
                files, tool_calls_used, telemetry = await self._generate_with_tools(
                    plan, layer, layer_name,
                    mode=panel_mode, aggregator=panel_aggregator,
                )
                if telemetry is not None:
                    last_panel_telemetry = telemetry
            else:
                files = await generator(plan, layer, self._llm)

            artifacts.update(files)
            # Preserve the layer mapping for the Reviewer.
            artifacts_by_layer[layer_name] = dict(files)

            if board is not None:
                payload: dict[str, Any] = {
                    "stage": self.name,
                    "layer": layer_name,
                    "files_generated": len(files),
                }
                if self._tool_use:
                    payload["tool_calls_used"] = tool_calls_used
                board.publish(STAGE_PROGRESS, payload)

        ctx["artifacts"] = artifacts
        ctx["artifacts_by_layer"] = artifacts_by_layer
        if last_panel_telemetry is not None:
            ctx["coder_panel"] = last_panel_telemetry
        return ctx

    # ── tool-use path (v2.0) ────────────────────────────────────────

    async def _generate_with_tools(
        self,
        plan: dict[str, Any],
        layer: dict[str, Any],
        layer_name: str,
        *,
        mode: str = "parallel",
        aggregator: str = "primary-wins",
    ) -> tuple[dict[str, str], int, dict[str, Any] | None]:
        """Drive the tool-loop for one layer.

        Returns ``(files, tool_calls_used, panel_telemetry)``.
        ``panel_telemetry`` is ``None`` when no panel is wired
        (preserving v2.0 ctx shape).

        Per layer we materialize a fresh :class:`tempfile.TemporaryDirectory`
        as the sandbox root; every fs/git/exec call is scoped to it. The
        directory is torn down on exit even if the LLM errors mid-loop.

        v2.2 panel wiring: when ``self._reasoning_panel`` is supplied,
        the FIRST round (the planning consultation) routes through
        :meth:`ReasoningPanel.consult`. The aggregated text becomes a
        synthetic assistant turn appended to the conversation so the
        ensuing tool-loop sees the panel's reasoning. The tool loop
        itself uses the single-provider ``chat_with_tools`` path
        unchanged — partner slots can't coherently emit tool_calls
        into the shared sandbox, so panel involvement deliberately
        ends after planning.

        Raises :class:`LayerGenerationError` if the LLM never produces a
        parseable final JSON object after the budget is exhausted.
        """
        user_prompt = build_user_prompt(plan, layer)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _TOOL_USE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        # v2.2: optional panel-driven planning round. We do a textual
        # consult (no tools) and graft the aggregated content into the
        # conversation as an assistant turn — the subsequent tool-loop
        # rounds run unchanged against the bare provider.
        panel_telemetry: dict[str, Any] | None = None
        if self._reasoning_panel is not None:
            planning_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "Before any tool calls, briefly outline your "
                        "approach for generating this layer's files. "
                        "Plain text only — no JSON, no tool calls."
                    ),
                },
            ]
            planning_text, panel_telemetry = await _chat_or_panel(
                self._llm, self._reasoning_panel,
                planning_messages,
                temperature=0.2, max_tokens=1024,
                mode=mode, aggregator=aggregator,
            )
            if planning_text:
                messages.append({"role": "assistant", "content": planning_text})

        budget = self._tool_call_budget
        tool_calls_used = 0
        last_raw_content: str = ""

        with tempfile.TemporaryDirectory(prefix="development-coder-tools-") as sandbox:
            sandbox_dir = Path(sandbox)
            while True:
                # If we're out of budget, force a final response (no tools).
                if tool_calls_used >= budget:
                    final = await self._llm.chat(
                        [
                            *messages,
                            {
                                "role": "user",
                                "content": (
                                    "Tool budget exhausted. Output your final "
                                    "JSON object now: keys are file paths, "
                                    "values are file contents."
                                ),
                            },
                        ],
                        temperature=0.0,
                    )
                    last_raw_content = final
                    break

                response = await self._llm.chat_with_tools(
                    messages, tools=TOOL_CATALOG
                )
                content = response.get("content")
                tool_calls = response.get("tool_calls") or []

                if tool_calls:
                    # Append the assistant turn (with tool_calls) and each
                    # tool result so the LLM sees the trace on the next call.
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content or "",
                            "tool_calls": tool_calls,
                        }
                    )
                    for tc in tool_calls:
                        if tool_calls_used >= budget:
                            break
                        result = await dispatch_tool_call(
                            tc.get("name", ""),
                            tc.get("arguments", {}) or {},
                            sandbox_dir,
                        )
                        tool_calls_used += 1
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "name": tc.get("name", ""),
                                "content": json.dumps(result),
                            }
                        )
                    continue  # loop and ask the LLM for its next move

                # No tool calls → the LLM thinks it's done. Use ``content``
                # as the final answer and parse it as a {path: content} dict.
                last_raw_content = content or ""
                break

        parsed = parse_llm_json(last_raw_content)
        if parsed is None:
            raise LayerGenerationError(layer_name, raw_response=last_raw_content)

        result_files: dict[str, str] = {}
        for path, body in parsed.items():
            if not isinstance(path, str):
                continue
            if isinstance(body, str):
                result_files[path] = body
            else:
                result_files[path] = json.dumps(body, indent=2)

        return result_files, tool_calls_used, panel_telemetry
