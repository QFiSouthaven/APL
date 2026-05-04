"""Round-Robin reviewer — alternate Stage 3 backed by a multi-LLM dialogue.

The single-pass :class:`~development.stages.reviewer.ReviewerStage` asks
one LLM for a verdict per layer. This alternate forwards each layer's
purpose + files to the round-robin peer service, which (when the
endpoint exists) orchestrates a 2-LLM Agent-A/Agent-B dialogue plus an
optional Agent-C implementer, returning an aggregated verdict in the
same shape we already consume:

    {"approved": bool, "issues": [str, ...], "request_regenerate": bool}

The integration is intentionally one-way (development → round-robin via
HTTP). We do NOT modify round-robin from this side.

Deferred mode
-------------
Round-robin's ``server.py`` does NOT currently expose a
``POST /api/review`` endpoint — the v2.0 reviewer is wired but lives in
*deferred mode* until that endpoint ships. While deferred:

* The reviewer probes round-robin's ``/api/health`` once per layer.
* On reachability failure the layer falls back to the single-pass
  :class:`ReviewerStage` machinery so the build never breaks.
* On reachability success but missing review endpoint we ALSO fall back
  to single-pass — and emit a STAGE_PROGRESS event with
  ``{"deferred": True, "reason": "no_review_endpoint",
  "round_robin_url": <url>}`` so observers can see exactly where the
  integration lives.

When round-robin grows the endpoint this stage will start using it
without further changes here (see :meth:`_post_review`).

ctx contract
------------
Reads ``ctx["artifacts_by_layer"]`` (per-layer view from the Coder)
and ``ctx["plan"]`` (Architect output). Sets:

* ``ctx["review"]``        — same shape as ReviewerStage.
* ``ctx["review_source"]`` — ``"round-robin"`` if the round-robin POST
  ever succeeded for any layer, else ``"round-robin-deferred"`` when
  every layer fell back to single-pass.
* ``ctx["artifacts"]``     — re-synced flat view, same as ReviewerStage.

The shared ``ctx["_reviewer_loopbacks"]`` budget — populated by either
reviewer — caps regeneration at ONE retry per layer per build.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from ..discovery import get_peer_url
from ..messageboard import MessageBoard
from ..stages.base import Stage
from ..stages.reviewer import ReviewerStage
from ..types import STAGE_PROGRESS

logger = logging.getLogger("development.reviewers.round_robin")


# Round-robin's review endpoint. Does not exist as of v2.0 — see the
# deferred-mode docstring above. Pinned here so flipping the integration
# on later is a one-line change.
ROUND_ROBIN_REVIEW_PATH = "/api/review"

# Health probe path; matches round-robin's existing endpoint exactly.
ROUND_ROBIN_HEALTH_PATH = "/api/health"

# HTTP timeouts. Generous because the round-robin dialogue is multi-LLM
# and may legitimately take 30+s to converge.
HEALTH_TIMEOUT_S = 5.0
REVIEW_TIMEOUT_S = 120.0


class RoundRobinReviewer(Stage):
    """Alternate Stage 3: forwards artifacts to round-robin for review.

    Substitutable for :class:`ReviewerStage` — same ``name`` so emitted
    events look identical to subscribers, same ctx contract, same
    bounded-loopback budget. Adds ``ctx["review_source"]`` so callers
    can tell which reviewer actually ran.
    """

    # Same name as ReviewerStage so STAGE_STARTED / STAGE_DONE event
    # streams aren't broken by the swap. ``review_source`` distinguishes
    # the two for callers that care.
    name: ClassVar[str] = "reviewer"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        artifacts: dict[str, dict[str, str]] = ctx.get("artifacts_by_layer") or {}
        plan: dict[str, Any] = ctx.get("plan") or {}
        board: MessageBoard | None = ctx.get("message_board")

        # Shared loopback budget with ReviewerStage. setdefault means
        # whichever reviewer constructs ctx first owns the set; both
        # cooperate on the SAME set object.
        ctx.setdefault("_reviewer_loopbacks", set())
        review: dict[str, dict[str, Any]] = {}

        if not artifacts:
            ctx["review"] = review
            ctx["review_source"] = "round-robin-deferred"
            return ctx

        url = get_peer_url("round_robin")

        # Quick reachability probe: if round-robin is down entirely, we
        # delegate the whole stage to ReviewerStage to keep the build
        # alive. This is NOT the deferred-endpoint path — that fallback
        # happens per-layer below.
        if not url or not await self._is_alive(url):
            logger.warning(
                "RoundRobinReviewer: round-robin peer unreachable at %r; "
                "delegating to single-pass ReviewerStage.",
                url,
            )
            fallback = ReviewerStage(self._llm)
            out = await fallback.run(ctx)
            out["review_source"] = "round-robin-deferred"
            return out

        plan_layers = plan.get("layers") or []
        layer_plans = {
            (lp.get("name") or "").lower(): lp
            for lp in plan_layers
            if isinstance(lp, dict)
        }

        any_round_robin_succeeded = False
        any_deferred = False

        for layer_name, files in artifacts.items():
            layer_obj = layer_plans.get(layer_name.lower(), {"name": layer_name})
            verdict, deferred = await self._review_layer(
                url, layer_name, layer_obj, files, board
            )
            regenerated = False

            if deferred:
                any_deferred = True
                # Per-layer fallback: hand THIS layer to a single-pass
                # critique. ReviewerStage._critique is a private helper
                # — instantiate the stage and call it.
                single = ReviewerStage(self._llm)
                verdict = await single._critique(layer_name, layer_obj, files)
            else:
                any_round_robin_succeeded = True

            # Bounded loopback — shared budget with ReviewerStage.
            loopbacks: set[str] = ctx["_reviewer_loopbacks"]
            if (
                verdict.get("request_regenerate")
                and not verdict.get("approved", False)
                and layer_name not in loopbacks
            ):
                # Reuse ReviewerStage's regen plumbing verbatim — it
                # owns the LayerGenerationError handling and the
                # feedback-kwarg fallback.
                helper = ReviewerStage(self._llm)
                new_files = await helper._try_regenerate(
                    layer_name, layer_obj, plan, verdict.get("issues") or []
                )
                if new_files is not None:
                    loopbacks.add(layer_name)
                    artifacts[layer_name] = new_files
                    regenerated = True
                    # Re-critique the regenerated artifacts. If the
                    # initial review came from round-robin, try
                    # round-robin again; if it was deferred, use
                    # single-pass.
                    if deferred:
                        verdict = await helper._critique(
                            layer_name, layer_obj, new_files
                        )
                    else:
                        verdict, redeferred = await self._review_layer(
                            url, layer_name, layer_obj, new_files, board
                        )
                        if redeferred:
                            verdict = await helper._critique(
                                layer_name, layer_obj, new_files
                            )

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
                        "review_source": (
                            "round-robin-deferred" if deferred else "round-robin"
                        ),
                    },
                )

        # Re-sync flat view for BuildResult.
        ctx["artifacts"] = {
            path: content
            for layer_files in artifacts.values()
            for path, content in layer_files.items()
        }
        ctx["review"] = review
        # If at least one layer round-tripped successfully we mark the
        # build as round-robin reviewed. If every layer fell back, the
        # source is the deferred sentinel so callers can see we never
        # actually exercised round-robin.
        if any_round_robin_succeeded:
            ctx["review_source"] = "round-robin"
        else:
            ctx["review_source"] = "round-robin-deferred"
        # Quiet the linter — any_deferred is observable via review_source.
        del any_deferred
        return ctx

    # ── HTTP helpers ────────────────────────────────────────────────

    async def _is_alive(self, url: str) -> bool:
        """Probe ``GET {url}/api/health`` once. Never raises."""
        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_S) as client:
                resp = await client.get(f"{url}{ROUND_ROBIN_HEALTH_PATH}")
                return resp.status_code == 200
        except httpx.HTTPError as exc:
            logger.debug("RoundRobinReviewer: health probe failed: %s", exc)
            return False

    async def _review_layer(
        self,
        url: str,
        layer_name: str,
        layer_obj: dict[str, Any],
        files: dict[str, str],
        board: MessageBoard | None,
    ) -> tuple[dict[str, Any], bool]:
        """POST a layer to round-robin's review endpoint.

        Returns ``(verdict, deferred)``. When ``deferred=True`` the
        verdict is empty and the caller should fall back to single-pass.
        Reasons for deferral:

        * Round-robin returns 404 — endpoint doesn't exist yet.
        * Any HTTP/transport error talking to round-robin.

        Reachability of round-robin overall is checked once at the top
        of :meth:`run`; this method assumes the peer is up.
        """
        verdict, deferred = await self._post_review(url, layer_name, layer_obj, files)

        if deferred and board is not None:
            board.publish(
                STAGE_PROGRESS,
                {
                    "stage": self.name,
                    "layer": layer_name,
                    "deferred": True,
                    "reason": "no_review_endpoint",
                    "round_robin_url": url,
                },
            )

        return verdict, deferred

    async def _post_review(
        self,
        url: str,
        layer_name: str,
        layer_obj: dict[str, Any],
        files: dict[str, str],
    ) -> tuple[dict[str, Any], bool]:
        """Issue the actual POST. Catches ALL HTTP errors; never raises.

        When round-robin grows ``POST /api/review`` returning a verdict
        in our standard shape, the only thing that needs updating is the
        endpoint path constant (``ROUND_ROBIN_REVIEW_PATH``) and any
        request-body schema tweaks below.
        """
        body = {
            "layer": layer_name,
            "purpose": layer_obj.get("purpose") or "",
            "files": files,
        }
        try:
            async with httpx.AsyncClient(timeout=REVIEW_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{url}{ROUND_ROBIN_REVIEW_PATH}", json=body
                )
        except httpx.HTTPError as exc:
            logger.debug(
                "RoundRobinReviewer: POST %s for layer %r failed: %s; "
                "deferring to single-pass.",
                ROUND_ROBIN_REVIEW_PATH,
                layer_name,
                exc,
            )
            return {}, True

        if resp.status_code == 404:
            # Endpoint doesn't exist yet — the documented v2.0 deferred
            # path. Caller falls back to single-pass for this layer.
            logger.info(
                "RoundRobinReviewer: %s returned 404 for layer %r; "
                "round-robin's review endpoint is not implemented yet "
                "(v2.0 deferred mode).",
                ROUND_ROBIN_REVIEW_PATH,
                layer_name,
            )
            return {}, True

        if resp.status_code >= 400:
            logger.warning(
                "RoundRobinReviewer: %s returned %d for layer %r; "
                "deferring to single-pass.",
                ROUND_ROBIN_REVIEW_PATH,
                resp.status_code,
                layer_name,
            )
            return {}, True

        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning(
                "RoundRobinReviewer: %s returned non-JSON for layer %r (%s); "
                "deferring to single-pass.",
                ROUND_ROBIN_REVIEW_PATH,
                layer_name,
                exc,
            )
            return {}, True

        if not isinstance(data, dict):
            return {}, True

        # Normalize the verdict into our canonical shape, mirroring
        # ReviewerStage._normalize_verdict's leniency rules.
        approved = bool(data.get("approved", True))
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = []
        issues = [str(i) for i in issues]
        request_regenerate = bool(data.get("request_regenerate", False))

        verdict = dict(data)
        verdict["approved"] = approved
        verdict["issues"] = issues
        verdict["request_regenerate"] = request_regenerate
        return verdict, False
