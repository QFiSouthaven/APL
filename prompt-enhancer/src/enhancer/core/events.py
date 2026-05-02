"""Frozen event contract — the standalone's API boundary.

Every event the pipeline emits (via the ``on_event`` callback) is named
here. Adding a new event is fine. **Renaming or repurposing an existing
event is a v2 migration** — the swarm-agent-dev monolith and the analytics
dashboard read this contract.

Payload schemas are documented in docstrings; no enforcement at runtime
(Python's duck typing keeps the hot path cheap). For static checking, see
the typed payload dataclasses below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """All public pipeline events. ``str`` mixin keeps JSON-serialization easy."""

    # ── pipeline backbone ─────────────────────────────────────────────
    AGENT_STEP             = "agent_step"
    AGENT_PASS_START       = "agent_pass_start"
    AGENT_PASS_CHUNK       = "agent_pass_chunk"
    AGENT_PASS_RESULT      = "agent_pass_result"
    AGENT_PIPELINE_SUMMARY = "agent_pipeline_summary"
    ENHANCEMENT_SCORE      = "enhancement_score"
    AGENT_DONE             = "agent_done"
    AGENT_ERROR            = "agent_error"

    # ── interactive disambiguation ────────────────────────────────────
    AGENT_DISAMBIGUATE     = "agent_disambiguate"

    # ── persona ──────────────────────────────────────────────────────
    PERSONA_START          = "persona_start"
    PERSONA_RESULT         = "persona_result"

    # ── magnitude transform ───────────────────────────────────────────
    MAGNITUDE_START        = "magnitude_start"
    MAGNITUDE_CHUNK        = "magnitude_chunk"
    MAGNITUDE_DONE         = "magnitude_done"
    MAGNITUDE_ERROR        = "magnitude_error"

    # ── skeleton of thought ──────────────────────────────────────────
    SOT_START              = "sot_start"
    SOT_CHUNK              = "sot_chunk"
    SOT_DONE               = "sot_done"
    SOT_ERROR              = "sot_error"

    # ── pretrial (model recommendation) ───────────────────────────────
    PRETRIAL_START         = "pretrial_start"
    PRETRIAL_RESULT        = "pretrial_result"
    PRETRIAL_ERROR         = "pretrial_error"

    # ── sessions ──────────────────────────────────────────────────────
    SESSION_CREATED        = "session_created"
    SESSION_LIST           = "session_list"
    SESSION_LOADED         = "session_loaded"
    SESSION_RENAMED        = "session_renamed"
    SESSION_CLEARED        = "session_cleared"
    SESSION_DELETED        = "session_deleted"
    SESSION_ENTRY_ADDED    = "session_entry_added"
    SESSION_ACTIVE         = "session_active"


# ── canonical task types & techniques ────────────────────────────────

CANONICAL_TASK_TYPES: frozenset[str] = frozenset(
    {"creative", "analytical", "factual", "instructional", "conversational", "coding"}
)
CANONICAL_TECHNIQUES: frozenset[str] = frozenset({"precision", "context", "structure"})


# ── typed payload helpers (optional — most callers pass kwargs) ───────

@dataclass(frozen=True)
class PassResult:
    """Payload for AGENT_PASS_RESULT.

    Pass 1 carries ``task_type``; Pass 2 carries ``technique``;
    Pass 4 carries ``scores`` (dict).
    """

    pass_number: int
    pass_name: str
    content: str
    model: str
    duration_ms: int
    task_type: str | None = None
    technique: str | None = None
    scores: dict[str, int] | None = None


@dataclass(frozen=True)
class Scores:
    """Pass 4 quality scores."""

    specificity: int   # 1–10
    constraints: int   # 1–10
    actionability: int # 1–10
    improvement: int   # 0–100

    def to_dict(self) -> dict[str, int]:
        return {
            "specificity": self.specificity,
            "constraints": self.constraints,
            "actionability": self.actionability,
            "improvement": self.improvement,
        }


P4_DEFAULTS: dict[str, int] = {
    "specificity": 5,
    "constraints": 5,
    "actionability": 5,
    "improvement": 50,
}


# ── result envelope returned from run_pipeline ────────────────────────

@dataclass(frozen=True)
class PipelineResult:
    """What ``run_pipeline()`` returns to the caller (CLI, UI, API)."""

    result: str                         # the enhanced prompt
    technique: str                      # canonical technique
    task_type: str                      # canonical task type
    scores: dict[str, int]              # P4 scores or P4_DEFAULTS
    scores_fallback: bool               # true if scoring was skipped/failed
    pass3_partial: bool                 # true if Pass 3 fell back to original
    persona: str | None                 # persona text if persona_mode was on
    magnitude_output: str               # may be empty
    sot_output: str                     # may be empty
    pass_times_ms: dict[str, int]       # per-pass durations
    model: str                          # primary model used
    scorer_model: str                   # P4 model (may equal model)
    run_id: str                         # uuid persisted to DB
    extras: dict[str, Any] | None = None
