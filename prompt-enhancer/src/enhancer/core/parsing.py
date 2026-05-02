"""Parse helpers + numeric clamps.

Lifted from ``swarm-agent-dev/src/webui/mods/agent_pipeline.py`` lines
33-47 (``_clamp``) and 368-513 (parsers, weakness counter, disambiguate
question parser). Behaviour is preserved verbatim — including the
"last occurrence wins" rule in :func:`parse_scores` — because the
analytics dashboard and ``devflow.py`` consumer in the monolith rely on
the existing semantics.

Public surface:

* :func:`clamp` — coerce-and-clamp with a default fallback.
* :func:`parse_task_type` — extract canonical task type from Pass 1 output.
* :func:`parse_technique` — extract PRIMARY FOCUS from Pass 2 output.
* :func:`parse_persona` — extract PERSONA line.
* :func:`parse_scores` — extract Pass 4 scores with defaults on failure.
* :func:`count_weakness_fields` — disambiguation trigger.
* :func:`parse_disambiguate_questions` — Q/A multiple-choice parser.
* :func:`coerce_task_type_for_code` — apply the post-processing override
  from agent_pipeline.py:1132-1136 that maps "instructional" + code-like
  prompt to "coding".
"""

from __future__ import annotations

from typing import Any

from .events import CANONICAL_TASK_TYPES, CANONICAL_TECHNIQUES, P4_DEFAULTS


# ── numeric clamp ─────────────────────────────────────────────────────

def clamp(val: Any, lo: float, hi: float, default: float) -> float:
    """Coerce ``val`` to a float in ``[lo, hi]``; return ``default`` on failure."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if f < lo:
        return lo
    if f > hi:
        return hi
    return f


# ── line-based parsers ────────────────────────────────────────────────

def parse_technique(text: str) -> str:
    """Extract PRIMARY FOCUS value from weakness-detection output.

    Returns one of ``{"precision", "context", "structure"}``; default is
    "precision" when missing or invalid.
    """
    for line in text.splitlines():
        if line.upper().startswith("PRIMARY FOCUS:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in CANONICAL_TECHNIQUES:
                return val
    return "precision"


def parse_task_type(text: str) -> str:
    """Extract TASK TYPE and normalize to a canonical type.

    LLMs often return noisy values like "instructional (creating a
    feature)" or "analytical|instructional". We extract the *first*
    canonical keyword by **position in the raw string** (not iteration
    order over the canonical set — that gave non-deterministic results
    when the model emitted multiple keywords).

    ``"coding"`` is NOT recognized here — it is applied as a post-
    processing override by :func:`coerce_task_type_for_code` when an
    "instructional" task contains code-like signals.
    """
    canonical_for_parse = CANONICAL_TASK_TYPES - {"coding"}
    for line in text.splitlines():
        if line.upper().startswith("TASK TYPE:"):
            raw = line.split(":", 1)[1].strip().lower()
            if raw in canonical_for_parse:
                return raw
            # Find the canonical keyword whose first occurrence is leftmost
            # in the raw string. This makes "analytical|instructional"
            # always parse as "analytical".
            best: tuple[int, str] | None = None
            for ctype in canonical_for_parse:
                idx = raw.find(ctype)
                if idx < 0:
                    continue
                if best is None or idx < best[0]:
                    best = (idx, ctype)
            return best[1] if best else raw
    return ""


# Code-keyword signal used to upgrade "instructional" → "coding" post-hoc
# (matches agent_pipeline.py:1132-1136 logic).
_CODE_KEYWORDS: tuple[str, ...] = ("code", "function", "api", "class", "implement")


def coerce_task_type_for_code(task_type: str, prompt: str) -> str:
    """Apply the "instructional + code keywords → coding" override.

    Mirrors the inline logic at agent_pipeline.py:1132-1136. Keep here so
    the standalone has a single, named function for the rule.
    """
    if task_type == "instructional":
        prompt_lower = prompt.lower()
        if any(kw in prompt_lower for kw in _CODE_KEYWORDS):
            return "coding"
    return task_type


def parse_persona(text: str) -> str:
    """Extract PERSONA value from persona-detection output. Empty on miss."""
    for line in text.splitlines():
        if line.upper().startswith("PERSONA:"):
            return line.split(":", 1)[1].strip()
    return ""


def parse_scores(text: str) -> dict[str, int]:
    """Parse quality scores from Pass 4 output; defaults on parse failure.

    ``{specificity, constraints, actionability, improvement}`` — last
    occurrence wins (no early break) to preserve verbatim behavior.
    """
    result: dict[str, int] = {}
    for line in text.splitlines():
        for key in ("specificity", "constraints", "actionability", "improvement"):
            if line.upper().startswith(key.upper() + ":"):
                try:
                    result[key] = int(line.split(":", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    result[key] = P4_DEFAULTS[key]
    return {k: result.get(k, P4_DEFAULTS[k]) for k in P4_DEFAULTS}


# ── weakness-count + disambiguate Q/A parsers ────────────────────────

def count_weakness_fields(pass2_text: str) -> int:
    """Count non-trivial weakness fields from Pass 2 output.

    A field is "trivial" when its value is empty / "none" / "n/a" /
    "none found". When this count reaches the disambiguation threshold
    (default 3), the pipeline pauses for clarification.
    """
    count = 0
    for key in ("VAGUE TERMS:", "MISSING CONTEXT:",
                "UNSTATED CONSTRAINTS:", "SCOPE ISSUES:"):
        for line in pass2_text.splitlines():
            if line.upper().startswith(key):
                val = line.split(":", 1)[1].strip().lower()
                if val and val not in ("none", "n/a", "none found", ""):
                    count += 1
                break
    return count


def parse_disambiguate_questions(text: str) -> list[dict]:
    """Parse Q1/Q2/Q3 + A/B/C options from disambiguate output.

    Single-character option markers only ("A)", "B)", "10)" is *not*
    recognized — by design; max 2-3 options per question).
    """
    questions: list[dict] = []
    current_q: dict | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(("Q1:", "Q2:", "Q3:")):
            if current_q:
                questions.append(current_q)
            current_q = {
                "question": stripped.split(":", 1)[1].strip(),
                "options": [],
            }
        elif current_q and len(stripped) >= 3 and stripped[1] == ")":
            current_q["options"].append(stripped[3:].strip())
    if current_q and current_q.get("options"):
        questions.append(current_q)
    return questions
