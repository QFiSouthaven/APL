"""Architect — stage 1: produce a structured stack design from a goal.

The Architect is the only stage shipped end-to-end in v0.1. It takes
the raw user goal + optional hints and asks the LLM for a JSON plan.
Downstream stages (Coder, Reviewer, …) consume that plan from
``ctx["plan"]``.

Failure handling is deliberately strict: garbage JSON gets one retry
with a tighter "valid JSON only" reminder; if that retry still fails,
:class:`ArchitectFailedError` is raised carrying the raw response so
the caller can show it for debugging instead of just "stage failed".
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from .._json_utils import parse_llm_json
from ..templates import discover_templates
from ..types import STAGE_PROGRESS, ArchitectFailedError, BuildRequest
from .base import Stage, _chat_or_panel

logger = logging.getLogger("development.stages.architect")


# System prompt the Architect sends. Kept here as a module constant so
# the test suite can pin it byte-for-byte and so it's easy to bump for
# prompt experiments.
SYSTEM_PROMPT = (
    "You are a software architect. Given the goal, produce a JSON object "
    "with keys: `stack` (object: frontend, backend, database, deployment), "
    "`layers` (array of {name, purpose, language, files}), `dependencies` "
    "(array of strings), `constraints_satisfied` (object). Output ONLY the "
    "JSON, no prose."
)


# Strict-retry reminder appended after a parse failure.
RETRY_REMINDER = (
    "Your previous response could not be parsed as JSON. Output ONLY valid "
    "JSON matching the requested schema. No markdown fences, no commentary."
)


class ArchitectStage(Stage):
    """Stage 1: ask the LLM for a structured stack plan."""

    name: ClassVar[str] = "architect"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        request: BuildRequest = ctx["build_request"]

        # v2.0: stack-template fast path. If the request's stack_hint
        # matches a registered template, skip the LLM call entirely and
        # use the template's pre-built plan. This shaves ~30s off builds
        # whose stack is well-known (FastAPI+SQLite, Express+Postgres,
        # etc.). Templates are discovered fresh per call — entry-points
        # are cheap to read and per-call discovery means new packages
        # become visible without restarting the server.
        hint = (request.stack_hint or "").lower()
        if hint:
            for tpl_cls in discover_templates().values():
                try:
                    tpl = tpl_cls()
                    if tpl.matches(hint):
                        plan = _normalize_plan(tpl.build_plan(request))
                        ctx["plan"] = plan
                        ctx["plan_source"] = f"template:{tpl.name}"
                        board = ctx.get("message_board")
                        if board is not None:
                            try:
                                board.publish(STAGE_PROGRESS, {
                                    "stage": "architect",
                                    "source": "template",
                                    "template": tpl.name,
                                })
                            except Exception as pub_exc:  # pragma: no cover
                                logger.warning(
                                    "Architect: STAGE_PROGRESS publish failed: %s",
                                    pub_exc,
                                )
                        return ctx
                except Exception as exc:
                    logger.warning(
                        "Template %s skipped: %s", tpl_cls.__name__, exc,
                    )
                    continue

        # Fall through to the existing LLM-driven path.
        user_prompt = _build_user_prompt(request)

        # Panel mode/aggregator come from the BuildRequest when a
        # ``reasoning_panel`` was wired into this stage. v2.2: Architect
        # routes its single planning call through the panel when one
        # exists; the aggregated text is parsed exactly as the bare
        # provider response would be, and per-slot raw outputs are
        # surfaced in ``ctx["architect_panel"]`` for observability.
        panel_mode = getattr(request, "panel_mode", None) or "parallel"
        panel_aggregator = (
            getattr(request, "panel_aggregator", None) or "primary-wins"
        )

        # First attempt.
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        panel_telemetry: dict[str, Any] | None = None
        raw, panel_telemetry = await _chat_or_panel(
            self._llm, self._reasoning_panel,
            messages, temperature=0.2, max_tokens=2048,
            mode=panel_mode, aggregator=panel_aggregator,
        )
        plan = parse_llm_json(raw)

        # One retry on parse failure, with a tighter reminder.
        if plan is None:
            logger.warning(
                "Architect: first response failed to parse; retrying once. "
                "Raw=%r",
                raw[:200],
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": raw},
                {"role": "user", "content": RETRY_REMINDER},
            ]
            raw, panel_telemetry = await _chat_or_panel(
                self._llm, self._reasoning_panel,
                messages, temperature=0.0, max_tokens=2048,
                mode=panel_mode, aggregator=panel_aggregator,
            )
            plan = parse_llm_json(raw)

        if plan is None:
            raise ArchitectFailedError(
                "Architect produced unparseable JSON after retry.",
                raw_response=raw,
            )

        # Best-effort schema sanity check; missing keys are filled with empties
        # rather than raising so simple goals don't have to populate every field.
        plan = _normalize_plan(plan)
        ctx["plan"] = plan
        if panel_telemetry is not None:
            ctx["architect_panel"] = panel_telemetry
        return ctx


# ── helpers ────────────────────────────────────────────────────────


def _build_user_prompt(request: BuildRequest) -> str:
    """Render the user-message body from a BuildRequest."""
    parts = [f"Goal: {request.goal}"]
    if request.stack_hint:
        parts.append(f"Preferred stack: {request.stack_hint}")
    if request.target_lang:
        parts.append(f"Target language: {request.target_lang}")
    if request.constraints:
        parts.append(f"Constraints: {json.dumps(request.constraints)}")
    return "\n".join(parts)


# Back-compat alias — kept so any code or test importing
# ``_try_parse_json`` from this module continues to work after the
# refactor that moved the parser into ``development._json_utils``.
_try_parse_json = parse_llm_json


def _normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Fill in the canonical schema keys with empty defaults if missing.

    Lets downstream stages assume keys exist without having to defensively
    ``.get(...)`` everywhere. Doesn't drop unexpected keys — anything the
    LLM added is preserved verbatim.
    """
    plan.setdefault("stack", {})
    plan.setdefault("layers", [])
    plan.setdefault("dependencies", [])
    plan.setdefault("constraints_satisfied", {})
    return plan
