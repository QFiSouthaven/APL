"""Helper: post a build artifact to round-robin (best-effort).

The architecture diagram has development → round-robin as the path for
"final outcome" review. Round-robin's REST surface today exposes
``/api/run/start`` to kick off a new dialogue and ``/api/peers`` for
discovery, but does not currently have a dedicated "ingest a build
artifact" endpoint.

For v0.1 this helper just smoke-checks the round-robin peer is up
(``GET /api/health``). The actual artifact-forwarding wiring is a
v2.x follow-up — once round-robin grows a dedicated ``/api/inbox`` (or
similar) endpoint, swap the body of :func:`forward_artifact` to POST
there.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..discovery import get_peer_url

logger = logging.getLogger("development.integrations.round_robin")


async def is_alive(*, timeout: float = 5.0) -> bool:
    """Return True iff round-robin's ``/api/health`` returns 200.

    Returns False on any error — peer down, no URL, network failure.
    Never raises.
    """
    url = get_peer_url("round_robin")
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/api/health")
            return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.debug("round-robin health probe failed: %s", exc)
        return False


async def forward_artifact(
    artifact: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Send ``artifact`` to round-robin for review.

    v0.1 stub: only verifies round-robin is reachable; does not actually
    post the artifact (round-robin lacks a dedicated ingest endpoint).
    Returns a status dict so the caller can record the outcome on the
    message board.
    """
    alive = await is_alive(timeout=timeout)
    if not alive:
        return {
            "forwarded": False,
            "reason": "round_robin_unreachable",
            "peer_url": get_peer_url("round_robin"),
        }
    return {
        "forwarded": False,
        "reason": "endpoint_not_yet_implemented_v2x",
        "peer_url": get_peer_url("round_robin"),
        "artifact_size": len(artifact),
    }
