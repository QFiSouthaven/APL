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
from ..types import ArchitectFailedError, BuildRequest
from .base import Stage

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
        user_prompt = _build_user_prompt(request)

        # First attempt.
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        raw = await self._llm.chat(messages, temperature=0.2, max_tokens=2048)
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
            raw = await self._llm.chat(messages, temperature=0.0, max_tokens=2048)
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
