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
import re
from typing import Any, ClassVar

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
        plan = _try_parse_json(raw)

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
            plan = _try_parse_json(raw)

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


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _try_parse_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction.

    Handles three common LLM tics:
      1. Plain JSON — parse directly.
      2. ```json fenced — strip fences and retry.
      3. Prose-wrapped JSON — find the first ``{`` … last ``}`` and try
         that substring.

    Returns the parsed dict, or ``None`` if nothing parses cleanly.
    """
    if not raw:
        return None
    text = raw.strip()

    # Direct parse.
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        pass

    # Strip code fences and retry.
    stripped = _FENCE_RE.sub("", text).strip()
    if stripped != text:
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            pass

    # First ``{`` to last ``}`` substring.
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = text[first : last + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            pass

    return None


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
