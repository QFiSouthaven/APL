"""Reviewer registry — pluggable Stage-3 implementations.

v0.3 shipped a single :class:`~development.stages.reviewer.ReviewerStage`
that asks one LLM for a per-layer verdict. v2.0 adds an alternate
:class:`RoundRobinReviewer` that forwards each layer's artifacts to the
round-robin peer for a multi-LLM dialogue review.

Both reviewers expose the same Stage interface, set the same
``ctx["review"]`` shape, and share the same ``ctx["_reviewer_loopbacks"]``
budget — at most one regen per layer per build, regardless of which
reviewer ran. That last point is load-bearing: the Tester stage may
swap reviewers between runs, but a single layer can never be regen'd
twice in one build.

The orchestrator's per-build dispatch (see ``Orchestrator.build``)
materializes a build-specific stage list by substituting the configured
reviewer in place of the default ``ReviewerStage`` instance — see
``BuildRequest.reviewer`` for the wire-level field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..stages.reviewer import ReviewerStage
from .round_robin import RoundRobinReviewer

if TYPE_CHECKING:
    from ..stages.base import Stage


# Public registry. Keys are the wire values accepted by
# ``BuildRequest.reviewer``; values are the Stage classes the
# orchestrator instantiates.
REVIEWERS: dict[str, type] = {
    "single-pass": ReviewerStage,
    "round-robin": RoundRobinReviewer,
}


def get_reviewer(name: str) -> type:
    """Return the Stage class for the named reviewer.

    Raises
    ------
    KeyError
        If ``name`` is not a registered reviewer key. Callers should
        validate the name before calling — the orchestrator falls back
        to the default ``ReviewerStage`` if the request specifies an
        unknown reviewer (see ``Orchestrator.build``).
    """
    return REVIEWERS[name]


__all__ = ["REVIEWERS", "RoundRobinReviewer", "get_reviewer"]
