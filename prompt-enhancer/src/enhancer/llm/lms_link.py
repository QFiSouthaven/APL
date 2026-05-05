"""Runtime LM Studio base-URL override.

Lets the user point all inference at a different LM Studio host (e.g.
a beefier rig on the LAN, or LM Link bridging a remote machine) without
restarting or editing env vars. The override is persisted via
``SafeStorage`` so it survives across restarts.

Pattern intentionally avoids the monolith's load-at-import singleton —
we read on demand. The :class:`LMStudioProvider` calls
:func:`get_active_base_url` on every API call so toggling takes effect
immediately.

Source: lifted from
``swarm-agent-dev/src/webui/services/lms_link.py`` and adapted for
``platformdirs``-aware data paths.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import data_dir
from ..persistence.safestorage import SafeStorage

logger = logging.getLogger("enhancer.llm.lms_link")

_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9._\-]+(?::\d{1,5})?(?:/[A-Za-z0-9._\-/]*)?$"
)


def _store_path() -> Path:
    return data_dir() / "lms_link.json"


def get_override() -> str | None:
    """Return the currently-set override URL, or ``None`` if not set."""
    data = SafeStorage.load_json(_store_path(), {"base_url": None})
    url = data.get("base_url")
    if isinstance(url, str) and _URL_RE.match(url):
        return url.rstrip("/")
    return None


def get_active_base_url(default: str) -> str:
    """Return the active base URL — override if set, else ``default``."""
    return get_override() or default


def set_override(base_url: str | None) -> str | None:
    """Persist a new override.

    Pass ``None`` or an empty string to clear and revert to the default.
    Returns the now-active override (or ``None`` if cleared). Raises
    :class:`ValueError` on a malformed URL.
    """
    if not base_url:
        try:
            _store_path().unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("lms_link: failed to clear override: %s", exc)
        return None

    candidate = base_url.strip().rstrip("/")
    if not _URL_RE.match(candidate):
        raise ValueError(
            f"Invalid base URL: {base_url!r} "
            "(expected http[s]://host[:port][/path])"
        )
    SafeStorage.save_json(_store_path(), {"base_url": candidate})
    return candidate


# Alias matching the v1.2 multi-host wiring spec. ``set_override`` is the
# original swarm-agent name and remains the underlying contract; this
# alias is what CLI/UI call sites use so the public surface tracks the
# documented "active base URL" terminology.
set_active_base_url = set_override
