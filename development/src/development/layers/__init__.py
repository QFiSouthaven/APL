"""Layer-specific code generators.

Each generator is an ``async def generate(plan, layer, llm) -> dict[str, str]``
that turns the Architect's plan into a ``{path: content}`` map for one
horizontal slice (frontend, backend, database, deployment).

Registry contract (consumed by ``stages.coder.CoderStage``):

    LAYER_GENERATORS: dict[str, Callable[..., Awaitable[dict[str, str]]]]

Keys are lowercased layer names matching ``plan["layers"][i]["name"]``.
Layers with no matching key are skipped by the Coder (a STAGE_PROGRESS
event with ``skipped=True`` is published).

The four generators shipped here cover the Coder column of the Stage ×
Layer matrix (DEVELOPMENT_FRAMEWORK.md §4). The ``tests`` and ``docs``
columns are intentionally unhandled — the Tester and Packager stages
own those, per the matrix.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from . import backend, database, deployment, frontend

# Public registry. Keys are lowercased; CoderStage normalizes incoming
# layer names with ``.lower()`` before lookup.
LAYER_GENERATORS: dict[
    str,
    Callable[[dict[str, Any], dict[str, Any], Any], Awaitable[dict[str, str]]],
] = {
    "backend": backend.generate,
    "frontend": frontend.generate,
    "database": database.generate,
    "deployment": deployment.generate,
}


def applies_to(plan: dict[str, Any], layer_name: str) -> bool:
    """Return True if ``layer_name`` has a registered generator AND
    appears in ``plan["layers"]`` (case-insensitive).

    Lets callers cheaply check the (Coder, layer) cell of the matrix
    without iterating the registry themselves.
    """
    key = (layer_name or "").lower()
    if key not in LAYER_GENERATORS:
        return False
    for entry in plan.get("layers", []) or []:
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == key:
            return True
    return False


__all__ = [
    "LAYER_GENERATORS",
    "applies_to",
    "backend",
    "frontend",
    "database",
    "deployment",
]
