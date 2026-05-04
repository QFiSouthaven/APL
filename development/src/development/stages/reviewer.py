"""Reviewer — stage 3: critique the Coder's per-layer artifacts.

For each layer's generated files, the Reviewer asks the LLM whether the
code looks correct, complete, and free of obvious bugs. The verdict is a
small JSON object:

    {"approved": bool, "issues": [str, ...], "request_regenerate": bool}

When ``request_regenerate`` is true (and the layer has not yet been
regenerated this build), the Reviewer calls the matching layer generator
in :mod:`development.layers` with the issues passed as ``feedback``,
replaces the artifacts in ``ctx``, and re-runs the critique once.
A second consecutive rejection is logged and accepted as-is — the
loopback is *bounded to one retry per layer* per
``docs/DEVELOPMENT_FRAMEWORK.md`` §5 to prevent Coder ↔ Reviewer
ping-pong.

The Reviewer is best-effort quality control, not a hard gate: if the
LLM produces unparseable JSON twice in a row, the layer is treated as
approved so the build can proceed. The verdict (with whatever issues we
got) is still recorded in ``ctx["review"]`` for downstream consumers.

Reads ``ctx["artifacts_by_layer"]`` (populated by the Coder) which is
the per-layer view ``{layer_name: {path: content}}`` — distinct from the
flat ``ctx["artifacts"]`` (``{path: content}``) that ``BuildResult``
serializes. The Coder maintains both shapes; the Reviewer reads the
nested one because per-layer critique is the unit of review.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from .._json_utils import parse_llm_json
from ..layers import LAYER_GENERATORS
from ..messageboard import MessageBoard
from ..types import STAGE_PROGRESS, LayerGenerationError
from .base import Stage

logger = logging.getLogger("development.stages.reviewer")


# System prompt — pinned here so tests can assert byte-for-byte and
# prompt experiments are easy to bump.
SYSTEM_PROMPT = (
    "You are a strict code reviewer. Given the layer's purpose and the "
    "generated files, evaluate: is the code correct, complete for the "
    "stated purpose, and free of obvious bugs? Output JSON ONLY: "
    '`{"approved": bool, "issues": ["...", "..."], '
    '"request_regenerate": bool}`. `request_regenerate` should be true '
    "ONLY if `approved` is false AND the issues are mechanical/code-level "
    "(not architectural). No prose outside the JSON."
)


# Strict-retry reminder appended after a parse failure.
RETRY_REMINDER = (
    "Your previous response could not be parsed as JSON. Output ONLY a "
    "JSON object with keys `approved` (bool), `issues` (array of "
    "strings), and `request_regenerate` (bool). No markdown fences, "
    "no commentary."
)


# Default verdict used when the Reviewer's LLM cannot produce parseable
# JSON. Best-effort quality control: don't fail the build because the
# critic can't speak.
_FALLBACK_VERDICT: dict[str, Any] = {
    "approved": True,
    "issues": [],
    "request_regenerate": False,
}


class ReviewerStage(Stage):
    """Stage 3: per-layer critique with bounded one-retry loopback."""

    name: ClassVar[str] = "reviewer"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        # Read the per-layer view (Coder maintains both flat + nested).
        artifacts: dict[str, dict[str, str]] = ctx.get("artifacts_by_layer") or {}
        plan: dict[str, Any] = ctx.get("plan") or {}
        board: MessageBoard | None = ctx.get("message_board")

        # Per-build regen tracking. The orchestrator may pre-seed this
        # set; if not, we own it. Either way the contract is "any layer
        # name in the set has already been regenerated once and must
        # not be regenerated again."
        loopbacks: set[str] = ctx.setdefault("_reviewer_loopbacks", set())

        review: dict[str, dict[str, Any]] = {}

        if not artifacts:
            # Nothing to critique. Return an empty review dict so
            # downstream stages can still ``ctx["review"][layer]`` without
            # KeyError after a no-op Reviewer pass.
            ctx["review"] = review
            return ctx

        # Build a quick lookup of layer-name → layer-plan-object so we
        # can hand the original purpose to the critic prompt and the
        # regenerator's signature.
        plan_layers = plan.get("layers") or []
        layer_plans = {
            (lp.get("name") or "").lower(): lp
            for lp in plan_layers
            if isinstance(lp, dict)
        }

        for layer_name, files in artifacts.items():
            layer_obj = layer_plans.get(layer_name.lower(), {"name": layer_name})
            verdict = await self._critique(layer_name, layer_obj, files)
            regenerated = False

            # Bounded loopback: only if this layer hasn't been regen'd yet.
            if (
                verdict.get("request_regenerate")
                and not verdict.get("approved", False)
                and layer_name not in loopbacks
            ):
                new_files = await self._try_regenerate(
                    layer_name, layer_obj, plan, verdict.get("issues") or []
                )
                if new_files is not None:
                    loopbacks.add(layer_name)
                    artifacts[layer_name] = new_files
                    regenerated = True
                    # Re-critique ONCE.
                    verdict = await self._critique(layer_name, layer_obj, new_files)
                    if verdict.get("request_regenerate") and not verdict.get(
                        "approved", False
                    ):
                        logger.warning(
                            "Reviewer: layer %r still rejected after one "
                            "regenerate; accepting as-is per bounded-loopback "
                            "rule. Issues: %r",
                            layer_name,
                            verdict.get("issues"),
                        )
                # If new_files is None we couldn't regenerate (no matching
                # generator, or LayerGenerationError) — fall through with
                # the original verdict and the pre-loopback artifacts.

            review[layer_name] = verdict
            if board is not None:
                board.publish(
                    STAGE_PROGRESS,
                    {
                        "stage": self.name,
                        "layer": layer_name,
                        "approved": bool(verdict.get("approved", False)),
                        "issues_count": len(verdict.get("issues") or []),
                        "regenerated": regenerated,
                    },
                )

        # Re-sync the flat artifacts view in case the Reviewer regenerated
        # any layer. BuildResult serializes ``ctx["artifacts"]`` (flat),
        # so it must reflect the post-loopback state.
        ctx["artifacts"] = {
            path: content
            for layer_files in artifacts.values()
            for path, content in layer_files.items()
        }
        ctx["review"] = review
        return ctx

    # ── internal helpers ────────────────────────────────────────────

    async def _critique(
        self,
        layer_name: str,
        layer_obj: dict[str, Any],
        files: dict[str, str],
    ) -> dict[str, Any]:
        """Send one critique request, with one parse-retry on garbage.

        Always returns a dict with the verdict shape; on persistent
        parse failure returns the fallback "approved" verdict so the
        build can keep moving.
        """
        user_prompt = _build_user_prompt(layer_name, layer_obj, files)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        raw = await self._llm.chat(messages, temperature=0.2, max_tokens=1024)
        parsed = parse_llm_json(raw)

        if parsed is None:
            logger.warning(
                "Reviewer: layer %r produced unparseable verdict; "
                "retrying once. Raw=%r",
                layer_name,
                raw[:200],
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": raw},
                {"role": "user", "content": RETRY_REMINDER},
            ]
            raw = await self._llm.chat(messages, temperature=0.0, max_tokens=1024)
            parsed = parse_llm_json(raw)

        if parsed is None:
            logger.warning(
                "Reviewer: layer %r still unparseable after retry; "
                "treating as approved (best-effort fallback).",
                layer_name,
            )
            return dict(_FALLBACK_VERDICT)

        return _normalize_verdict(parsed)

    async def _try_regenerate(
        self,
        layer_name: str,
        layer_obj: dict[str, Any],
        plan: dict[str, Any],
        issues: list[str],
    ) -> dict[str, str] | None:
        """Invoke the matching layer generator with the reviewer's feedback.

        Returns the new artifact dict, or ``None`` if regeneration was
        skipped (no generator) or failed (``LayerGenerationError``).

        The generator signature is owned by the parallel Coder-stage
        agent — it *may* accept a ``feedback`` kwarg or *may not*. We try
        with it first; on ``TypeError`` we fall back to calling without
        the kwarg so older signatures still work.
        """
        gen = LAYER_GENERATORS.get(layer_name.lower())
        if gen is None:
            logger.info(
                "Reviewer: no layer generator for %r; skipping loopback.",
                layer_name,
            )
            return None

        try:
            try:
                return await gen(plan, layer_obj, self._llm, feedback=issues)
            except TypeError as exc:
                # The generator doesn't accept ``feedback`` — call without it.
                # We log at debug rather than warning because this is the
                # documented fallback path, not an error.
                logger.debug(
                    "Reviewer: layer generator for %r does not accept "
                    "`feedback` kwarg (%s); calling without it.",
                    layer_name,
                    exc,
                )
                return await gen(plan, layer_obj, self._llm)
        except LayerGenerationError as exc:
            logger.warning(
                "Reviewer: layer generator for %r raised "
                "LayerGenerationError during loopback: %s. "
                "Keeping pre-loopback artifacts.",
                layer_name,
                exc,
            )
            return None


# ── module-level helpers ────────────────────────────────────────────


def _build_user_prompt(
    layer_name: str,
    layer_obj: dict[str, Any],
    files: dict[str, str],
) -> str:
    """Render the critique user-message body."""
    purpose = layer_obj.get("purpose") or "(no purpose stated in plan)"
    parts = [
        f"Layer: {layer_name}",
        f"Purpose: {purpose}",
        "",
        "Files:",
    ]
    for path, content in files.items():
        parts.append(f"--- {path} ---")
        parts.append(content)
    return "\n".join(parts)


def _normalize_verdict(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce an LLM verdict into the canonical shape.

    The LLM can return missing/extra keys or wrong types. We enforce:
      * ``approved`` defaults to True (lenient — the critic must *prove*
        a problem to fail a layer)
      * ``issues`` defaults to []; non-list values are coerced to []
      * ``request_regenerate`` defaults to False
    Extra keys are preserved verbatim.
    """
    approved = bool(raw.get("approved", True))
    issues = raw.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    issues = [str(i) for i in issues]
    request_regenerate = bool(raw.get("request_regenerate", False))

    out = dict(raw)
    out["approved"] = approved
    out["issues"] = issues
    out["request_regenerate"] = request_regenerate
    return out
