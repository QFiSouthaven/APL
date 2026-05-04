"""Helper: forward a goal to the prompt-enhancer's ``/api/enhance`` endpoint.

Used when a user submits a raw, terse goal (e.g. "make me a todo app")
and we want a polished, multi-pass-enhanced prompt to feed into the
Architect. The peer URL is read fresh from ``services.toml`` every
call so users can move the prompt-enhancer to a different host without
restarting development.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..discovery import get_peer_url

logger = logging.getLogger("development.integrations.prompt_enhancer")


async def enhance(
    prompt: str,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Send ``prompt`` to prompt-enhancer; return the response JSON.

    On any error (peer down, non-2xx, malformed JSON) returns a fallback
    dict ``{"enhanced": prompt, "fallback": True, "error": "..."}`` so
    callers can keep building rather than fail the whole pipeline. The
    development service deliberately treats prompt-enhancer as a
    nice-to-have, not a hard dependency.
    """
    url = get_peer_url("prompt_enhancer")
    if not url:
        return {"enhanced": prompt, "fallback": True, "error": "peer_url_missing"}

    endpoint = f"{url}/api/enhance"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, json={"prompt": prompt})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("prompt-enhancer returned non-object response")
            return data
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("prompt-enhancer enhance call failed: %s", exc)
        return {"enhanced": prompt, "fallback": True, "error": str(exc)}
