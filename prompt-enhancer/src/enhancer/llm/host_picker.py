"""Glue between :func:`lms_discovery.pick_loaded_host` and the runtime
LM Studio base-URL override (:func:`lms_link.set_active_base_url`).

Both the typer CLI's global ``--lms-hosts`` flag (and the
``ENHANCER_LMS_HOSTS`` env var) and the Settings page's "Pick best host
now" button funnel through :func:`apply_host_pick`. Keeping the logic
in one place means the back-compat invariants — *no override on
failure, no behaviour change when the host list is empty* — only need
to be tested once.

This is the v1.2 multi-host wiring closure: ``pick_loaded_host`` was
shipped in v1.2 but never connected to anything user-facing.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .lms_discovery import pick_loaded_host
from .lms_link import set_active_base_url

logger = logging.getLogger("enhancer.llm.host_picker")


def parse_hosts(raw: str | None) -> list[str]:
    """Split a comma- or newline-separated host list into trimmed URLs.

    Accepts ``None`` (returns ``[]``), the CLI flag's comma form, the
    UI text-area's line form, or a mix. Empty/whitespace tokens are
    dropped so trailing commas are harmless.
    """
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        token = chunk.strip()
        if token:
            parts.append(token)
    return parts


async def apply_host_pick(
    hosts: Iterable[str],
    *,
    preferred_model: str | None = None,
) -> tuple[str | None, str | None]:
    """Probe ``hosts`` and, if any has a chat model loaded, set the
    active base URL to it.

    Returns ``(host_url, model_id)`` from :func:`pick_loaded_host` so
    callers can surface the chosen host in their UX. On failure
    (empty list or no responder) the existing override is left intact
    and a warning is logged — never raises.
    """
    host_list = [h for h in hosts if h]
    if not host_list:
        return None, None

    try:
        host, model = await pick_loaded_host(host_list, preferred_model=preferred_model)
    except Exception as exc:  # discovery is best-effort; never crash the CLI/UI
        logger.warning("apply_host_pick: pick_loaded_host failed: %s", exc)
        return None, None

    if host is None:
        logger.warning(
            "apply_host_pick: no host in %s has a loaded chat model; "
            "leaving active base URL untouched",
            host_list,
        )
        return None, None

    try:
        set_active_base_url(host)
    except ValueError as exc:
        # pick_loaded_host returned something the URL validator rejects.
        # Belt-and-braces — discovery hosts are user-supplied.
        logger.warning("apply_host_pick: set_active_base_url rejected %r: %s", host, exc)
        return None, None

    logger.info("apply_host_pick: active LM Studio base URL → %s (model %s)", host, model)
    return host, model
