"""Task-aware scorer model selection.

Pass 4 of the pipeline scores the rewrite. Today the user supplies
``scorer_model`` manually (and the pipeline falls back to the primary
model when nothing is set). This module provides a *pure* helper that
picks a sensible scorer based on the parsed Pass 1 ``task_type``.

Wiring into ``core/pipeline.py`` is a v1.2.x follow-up — this module
just delivers the routing logic and tests so the rules are auditable in
isolation.

## Heuristic routing table

The rules below are seeded from operator intuition (not benchmarks):
coder-tuned models tend to score analytical/coding rewrites more
strictly; Hermes/Mistral keep creative outputs from being graded down
for "lack of structure"; Qwen3 is a reasonable default for research-y
prompts. Substring matching on lowercased model IDs is intentional
because user model names vary ("qwen3-coder-30b-a3b-instruct",
"Qwen2.5-Coder-32B-Instruct-Q5_K_M", etc.).

| task_type        | substrings tried (priority order)                        |
| ---------------- | --------------------------------------------------------- |
| analytical       | qwen3-coder, deepseek-coder, qwen3, qwen, llama-3         |
| coding           | qwen3-coder, deepseek-coder, qwen3, qwen, llama-3         |
| research         | qwen3, deepseek, llama-3, qwen                            |
| factual          | qwen3, deepseek, llama-3, qwen                            |
| creative         | hermes, mistral, llama-3, qwen                            |
| instructional    | qwen3-coder, qwen, llama-3                                |
| conversational   | (no rules — alphabetic fallback)                          |
| (anything else)  | (no rules — alphabetic fallback)                          |

Note: the canonical task-type set lives in ``core/events.py``
(``CANONICAL_TASK_TYPES``); we mirror the six known values here but do
not import it to avoid a circular dep. Unknown types fall through to
``available_models[0]``.

Future work: replace ``_RULES`` with a config-loaded table (TOML) once
the user has empirical preferences.
"""

from __future__ import annotations

from typing import Final

# Substring rules. Lowercased. First-match-wins inside each task type.
# ``coding`` mirrors ``analytical`` because the post-processing override
# in ``core/parsing.coerce_task_type_for_code`` upgrades instructional →
# coding when code keywords are present.
_RULES: Final[dict[str, tuple[str, ...]]] = {
    "analytical":    ("qwen3-coder", "deepseek-coder", "qwen3", "qwen", "llama-3"),
    "coding":        ("qwen3-coder", "deepseek-coder", "qwen3", "qwen", "llama-3"),
    "research":      ("qwen3", "deepseek", "llama-3", "qwen"),
    "factual":       ("qwen3", "deepseek", "llama-3", "qwen"),
    "creative":      ("hermes", "mistral", "llama-3", "qwen"),
    "instructional": ("qwen3-coder", "qwen", "llama-3"),
}


def _first_or_empty(available_models: list[str]) -> str:
    """Return the alphabetically-first model id, or an empty string."""
    if not available_models:
        return ""
    # Caller may pass an unsorted list; mirror the sorted-output rule
    # used by lms_discovery (loaded-first, then alpha) by sorting on
    # lowered id here. We do not assume the caller pre-sorted.
    return sorted(available_models, key=str.lower)[0]


def _match_substring(substrings: tuple[str, ...], available_models: list[str]) -> str | None:
    """Return the first model whose lowercased id contains any of the
    substrings, scanning substrings in priority order. Within one
    substring, models are scanned in caller order (so a loaded-first
    sorted list keeps its preference).
    """
    lowered = [(m, m.lower()) for m in available_models]
    for sub in substrings:
        for original, low in lowered:
            if sub in low:
                return original
    return None


def select_scorer(
    task_type: str,
    available_models: list[str],
    preferred: str | None = None,
) -> str:
    """Pick a Pass 4 scorer model for ``task_type``.

    Resolution order:

    1. If ``preferred`` is set and present in ``available_models``,
       return it verbatim.
    2. Apply ``_RULES[task_type]`` (lowercased substring match, priority
       order). First hit wins.
    3. Otherwise return the alphabetic-first entry in ``available_models``
       (matching the existing fallback rule), or the empty string if
       the list is empty.

    Pure function. No I/O. Safe to call in hot paths.
    """
    if preferred and preferred in available_models:
        return preferred

    rules = _RULES.get(task_type.lower() if task_type else "")
    if rules:
        match = _match_substring(rules, available_models)
        if match is not None:
            return match

    return _first_or_empty(available_models)


def select_default(
    available_models: list[str],
    preferred: str | None = None,
) -> str:
    """Pick a default model with no task-type signal.

    ``preferred`` wins if it's available; otherwise alphabetic-first;
    empty string if the list is empty.
    """
    if preferred and preferred in available_models:
        return preferred
    return _first_or_empty(available_models)
