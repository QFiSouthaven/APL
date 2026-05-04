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

    ``reviewer`` (v2.0+) selects the Stage-3 implementation. Defaults to
    ``"single-pass"`` for backward compatibility — every pre-v2.0 caller
    hits the same ReviewerStage they always did. Pass ``"round-robin"``
    to route per-layer critique through the round-robin peer service.
    See ``development.reviewers.REVIEWERS`` for the registry.
    """

    goal: str
    stack_hint: str | None = None
    target_lang: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
    reviewer: str = "single-pass"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuildResult:
    """The full outcome of a build run, including non-fatal warnings.

    ``test_results`` is populated by the Tester stage (v0.4+) and maps
    each layer name to a result dict ``{status, duration_ms, num_passed,
    num_failed, runner, stdout_tail, stderr_tail, regenerated}``. Empty
    when the Tester didn't run or had no artifacts to test.

    ``package_validation`` is populated by the Packager stage (v0.5+)
    and maps each emitted packaging file path to a structural-validation
    dict ``{file, ok, issues}``. The Packager is informational, not a
    gate — entries with ``ok=False`` are warnings, not build failures.
    Empty when the Packager didn't run.
    """

    request: BuildRequest
    stages_completed: tuple[str, ...]
    artifacts: dict[str, str]
    plan: dict[str, Any]
    duration_ms: int
    errors: tuple[str, ...] = ()
    test_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    package_validation: dict[str, dict[str, Any]] = field(default_factory=dict)

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
STAGE_PROGRESS = "STAGE_PROGRESS"
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


class LayerGenerationError(RuntimeError):
    """Raised by a layer generator when the LLM never produces parseable JSON.

    The generator tries once, then retries once at temperature=0.0 with a
    "valid JSON only" reminder. If both fail, this is raised carrying the
    layer name and the last raw response so the Coder can decide whether
    to skip the layer or fail the whole build.
    """

    def __init__(self, layer_name: str, raw_response: str = "") -> None:
        super().__init__(f"Layer {layer_name!r} produced unparseable JSON after retry.")
        self.layer_name = layer_name
        self.raw_response = raw_response
