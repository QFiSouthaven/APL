"""Inter-product service discovery via ``services.toml``.

Mirror of ``prompt_enhancer.api.discovery`` — same lookup order, same
``DEFAULTS`` dict, same swallow-everything error policy. The two
products read the same shared TOML file at:

* Windows: ``%APPDATA%\\swarm\\services.toml``
* Linux/macOS: ``~/.config/swarm/services.toml``

so the four-product loop (Prompt Enhancer / Round Robin / Interpreter
/ Loop Driver) all agree on each other's URLs without imports between
packages. Each product reads on demand (no caching). Defaults to
localhost loopback ports so dev still works without the file present.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# Default localhost ports. Override via services.toml.
# IDENTICAL to prompt_enhancer.api.discovery.DEFAULTS — the two
# products must agree byte-for-byte on each other's locations.
DEFAULTS: dict[str, str] = {
    "prompt_enhancer": "http://127.0.0.1:8765",
    "round_robin": "http://127.0.0.1:8766",
    "development": "http://127.0.0.1:8767",
}


def services_path() -> Path:
    """Path to the shared services.toml; not auto-created."""
    return Path(user_config_dir("swarm", appauthor=False)) / "services.toml"


def get_peer_url(name: str, default: str | None = None) -> str:
    """Return the URL for a peer service.

    Lookup order:
      1. ``services.toml`` ``[services]`` section if the file exists.
      2. ``DEFAULTS`` for known peer names.
      3. The supplied ``default`` (or empty string if nothing matches).
    """
    path = services_path()
    if path.exists():
        try:
            with path.open("rb") as f:
                data: dict[str, Any] = tomllib.load(f)
            url = data.get("services", {}).get(name)
            if isinstance(url, str) and url.strip():
                return url.rstrip("/")
        except (tomllib.TOMLDecodeError, OSError):
            # Fall through to defaults; bad config shouldn't break startup.
            pass
    return (default or DEFAULTS.get(name) or "").rstrip("/")


def get_all_peers() -> dict[str, str]:
    """Return the full discovery table merged with defaults."""
    out = dict(DEFAULTS)
    path = services_path()
    if path.exists():
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
            for key, val in (data.get("services") or {}).items():
                if isinstance(val, str) and val.strip():
                    out[key] = val.rstrip("/")
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return out
