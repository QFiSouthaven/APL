"""Stack-template plugin discovery.

Templates register via the ``development.stack_templates`` entry-point
group. The Architect calls :func:`discover_templates` on each build to
fetch every registered template; if any matches the request's
``stack_hint`` it produces the plan directly, skipping the LLM call.

Mirrors prompt-enhancer's ``enhancer.providers`` / ``enhancer.transforms``
discovery patterns. The ``_iter_entry_points`` shim is copied (not
imported) from there — see ``_eps.py`` for the rationale.
"""

from __future__ import annotations

import logging
from typing import Any

from ._eps import _iter_entry_points
from .base import StackTemplate

_log = logging.getLogger(__name__)


def discover_templates() -> dict[str, type[StackTemplate]]:
    """Return ``{name: TemplateClass}`` for every registered template.

    Each entry is duck-checked: it must be a :class:`StackTemplate`
    subclass. Anything failing the check is logged and skipped — never
    raised. ``ep.load()`` failures are likewise logged + skipped, so a
    broken third-party plugin can't take the Architect down.
    """
    found: dict[str, type[StackTemplate]] = {}
    for ep in _iter_entry_points("development.stack_templates"):
        ep_name = getattr(ep, "name", None)
        if not ep_name:
            continue
        try:
            cls = ep.load()
        except Exception as exc:
            _log.warning(
                "Failed to load entry-point %r in group "
                "'development.stack_templates': %s",
                ep_name, exc,
            )
            continue
        if not (isinstance(cls, type) and issubclass(cls, StackTemplate)):
            _log.warning(
                "Entry-point %r in group 'development.stack_templates' is "
                "not a StackTemplate subclass; skipping.",
                ep_name,
            )
            continue
        found[ep_name] = cls
    return found


__all__ = ["StackTemplate", "discover_templates"]


# Re-export the shim so tests can monkeypatch it on the package level
# (matching prompt-enhancer's `registry._iter_entry_points` pattern).
_iter_entry_points: Any = _iter_entry_points
