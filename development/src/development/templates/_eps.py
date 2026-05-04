"""Compat shim for ``importlib.metadata.entry_points`` — copied verbatim
from prompt-enhancer's ``enhancer.llm.registry._iter_entry_points``.

Per the umbrella's path-injection rule (development → prompt-enhancer is
one-way), we duplicate this small helper here rather than importing
across products. Bidirectional fragility is the thing we're avoiding.
"""

from __future__ import annotations

import importlib.metadata as _im
from collections.abc import Iterable
from typing import Any


def _iter_entry_points(group: str) -> Iterable[Any]:
    """Compat shim for ``importlib.metadata.entry_points``.

    Python 3.10+ supports the ``group=`` keyword on ``entry_points()``,
    but the underlying return type and behavior tightened over 3.10 →
    3.12. The selectable / dict-style access from 3.9 is also still
    in the wild via shimmed backports. This helper tries the modern
    ``group=`` form first and falls back to the older
    ``entry_points()[group]`` form if the kwarg raises ``TypeError``
    (or anything else — better to fall through than to crash startup).
    """
    try:
        eps = _im.entry_points(group=group)
        return list(eps)
    except TypeError:
        # Older API: entry_points() returns a dict-like keyed by group.
        try:
            eps_all = _im.entry_points()
            return list(eps_all.get(group, []))  # type: ignore[union-attr]
        except Exception:  # pragma: no cover — defensive
            return []
    except Exception:  # pragma: no cover — defensive
        return []
