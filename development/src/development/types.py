"""Frozen value types for the build pipeline.

These are the wire format between the CLI/HTTP edge, the orchestrator,
the stages, and the message board. Keep them small and serializable —
the round-robin/prompt-enhancer integrations turn them into JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class BuildRequest:
    """A single user-driven build request.

    ``goal`` is required; everything else is optional and the Architect
    stage fills in defaults. ``constraints`` is an open dict so callers
    can attach things like ``{"max_loc": 500, "no_external_deps": True}``
    without requiring a pyproject change.
    """

    goal: str
    stack_hint: str | None = None
    target_lang: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuildResult:
    """The full outcome of a build run, including non-fatal warnings."""

    request: BuildRequest
    stages_completed: tuple[str, ...]
    artifacts: dict[str, str]
    plan: dict[str, Any]
    duration_ms: int
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # tuples → lists for JSON-friendliness
        d["stages_completed"] = list(self.stages_completed)
        d["errors"] = list(self.errors)
        return d


@dataclass(frozen=True)
class StageEvent:
    """Single event emitted to the message board.

    The orchestrator publishes ``BUILD_STARTED``, ``STAGE_STARTED``,
    ``STAGE_DONE``, and ``BUILD_DONE`` events for each step. Subscribers
    (round-robin's display, the /api/runs endpoint) consume these.
    """

    id: int
    ts: float
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Event-kind constants. Strings, not Enum, so the SQLite payload stays
# trivially serializable without the round-tripping ceremony.
BUILD_STARTED = "BUILD_STARTED"
STAGE_STARTED = "STAGE_STARTED"
STAGE_DONE = "STAGE_DONE"
STAGE_FAILED = "STAGE_FAILED"
BUILD_DONE = "BUILD_DONE"
BUILD_FAILED = "BUILD_FAILED"


class ArchitectFailedError(RuntimeError):
    """Raised when the Architect stage cannot produce a valid plan.

    Carries the raw LLM response so the caller can surface it for
    debugging instead of just logging an opaque "parse failure".
    """

    def __init__(self, message: str, *, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response
