"""Round-robin sibling handoff helper for the Studio page.

The Studio renders a "→ Round Robin" button next to the run-result panel.
On click, it POSTs the current run's enhanced prompt to the round-robin
sibling's ``/api/review`` endpoint and surfaces the verdict inline.

This module exposes two pure helpers (no NiceGUI imports):

* :func:`build_review_request` — constructs the ``ReviewVerdict`` request
  body. Pulled out so it's unit-testable without spinning up a UI.
* :func:`post_review` — async POST against the peer's ``/api/review``,
  using :func:`enhancer.api.discovery.get_peer_url` for lookup. Returns
  a small :class:`HandoffResult` dataclass that distinguishes between
  ``peer_missing``, ``unreachable``, ``http_error``, and ``ok``.

Failure modes are surfaced as fields on ``HandoffResult`` — never raised —
so the UI can map each into a notification without try/except gymnastics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ...api.discovery import get_peer_url


@dataclass(frozen=True)
class HandoffResult:
    """Outcome of a round-robin handoff call.

    Exactly one of ``verdict`` (200 path) or ``error`` (anything else) is
    populated. ``status`` lets the UI render a different notification per
    failure category without parsing the error string.
    """

    status: str  # "ok" | "peer_missing" | "unreachable" | "http_error"
    verdict: dict[str, Any] | None = None
    error: str = ""
    http_status: int = 0


def build_review_request(
    *, original_prompt: str, enhanced: str
) -> dict[str, Any]:
    """Construct the JSON body for the round-robin ``/api/review`` POST.

    Body shape (per the round-robin contract):
    ``{layer: str, purpose: str, files: dict[str, str]}``

    The Studio always treats an enhanced-prompt run as ``layer="prompt"``.
    The user's original prompt becomes the ``purpose`` so the reviewer
    knows the intent; the enhanced text is shipped as a single virtual
    file, ``enhanced.txt``.
    """
    # Trim "purpose" defensively — round-robin reviewers don't need a
    # 50KB user message; the substantive content lives in `files`.
    excerpt = (original_prompt or "").strip()
    if len(excerpt) > 500:
        excerpt = excerpt[:497] + "..."
    return {
        "layer": "prompt",
        "purpose": f"User asked: {excerpt}",
        "files": {"enhanced.txt": enhanced or ""},
    }


async def post_review(
    *,
    original_prompt: str,
    enhanced: str,
    peer_name: str = "round_robin",
    timeout: float = 30.0,
) -> HandoffResult:
    """POST the run's artifacts to the round-robin sibling's review endpoint.

    Lookup goes through :func:`enhancer.api.discovery.get_peer_url`, which
    consults ``services.toml`` and falls back to localhost defaults. If
    the peer name is missing from the discovery table AND has no built-in
    default, we treat that as ``peer_missing``.

    Network errors and non-2xx responses are caught and reported via the
    returned :class:`HandoffResult` so the UI can surface them as in-app
    notifications without unhandled exceptions.
    """
    peer_url = get_peer_url(peer_name)
    if not peer_url:
        return HandoffResult(
            status="peer_missing",
            error=f"{peer_name} sibling not in services.toml",
        )

    body = build_review_request(
        original_prompt=original_prompt, enhanced=enhanced
    )
    target = f"{peer_url}/api/review"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(target, json=body)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
        return HandoffResult(
            status="unreachable",
            error=f"{type(exc).__name__}: {exc}",
        )

    if resp.status_code >= 400:
        return HandoffResult(
            status="http_error",
            error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            http_status=resp.status_code,
        )

    try:
        verdict = resp.json()
    except ValueError as exc:
        return HandoffResult(
            status="http_error",
            error=f"non-JSON response: {exc}",
            http_status=resp.status_code,
        )

    return HandoffResult(
        status="ok",
        verdict=verdict if isinstance(verdict, dict) else {"raw": verdict},
        http_status=resp.status_code,
    )


# ── persona handoff ────────────────────────────────────────────────────


def build_persona_handoff_request(
    *, theme: str, alpha_persona: str, bravo_persona: str
) -> dict[str, Any]:
    """Construct the JSON body for round-robin's ``/api/persona-handoff``.

    Wire contract (per the Round-Robin v1.x spec):

    ``{theme: str, alpha_persona: str, bravo_persona: str, source: str}``

    ``theme`` carries the enhanced prompt (the conversation seed).
    ``alpha_persona`` / ``bravo_persona`` are the two persona system
    messages — Persona A is the primary (always present), Persona B is
    the optional partner (the helper does NOT policy-block an empty
    bravo; the UI decides whether to warn). ``source`` is fixed so the
    receiver can route by origin product.
    """
    return {
        "theme": theme or "",
        "alpha_persona": alpha_persona or "",
        "bravo_persona": bravo_persona or "",
        "source": "prompt-enhancer",
    }


async def post_persona_handoff(
    *,
    theme: str,
    alpha_persona: str,
    bravo_persona: str,
    peer_name: str = "round_robin",
    timeout: float = 30.0,
) -> HandoffResult:
    """POST the two personas + theme to round-robin's ``/api/persona-handoff``.

    Mirrors :func:`post_review`: same lookup via
    :func:`enhancer.api.discovery.get_peer_url`, same ``HandoffResult``
    failure taxonomy (``ok`` / ``peer_missing`` / ``unreachable`` /
    ``http_error``). Unlike the review path, the success state has no
    "verdict" — round-robin simply acknowledges receipt — so on 200 we
    return ``HandoffResult(status="ok")`` and the UI renders no panel.

    The helper does not policy-block an empty ``bravo_persona``; the UI
    layer is responsible for warning the user that Bravo will keep its
    pre-existing persona.
    """
    peer_url = get_peer_url(peer_name)
    if not peer_url:
        return HandoffResult(
            status="peer_missing",
            error=f"{peer_name} sibling not in services.toml",
        )

    body = build_persona_handoff_request(
        theme=theme,
        alpha_persona=alpha_persona,
        bravo_persona=bravo_persona,
    )
    target = f"{peer_url}/api/persona-handoff"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(target, json=body)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
        return HandoffResult(
            status="unreachable",
            error=f"{type(exc).__name__}: {exc}",
        )

    if resp.status_code >= 400:
        return HandoffResult(
            status="http_error",
            error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            http_status=resp.status_code,
        )

    # Persona handoff is fire-and-acknowledge: no verdict to surface.
    return HandoffResult(status="ok", http_status=resp.status_code)
