"""Shared LLM JSON-extraction helpers.

Both the Architect stage and the per-layer Coder generators ask the LLM
for a JSON object and have to be tolerant of three common LLM tics:

  1. Plain JSON — ``{"foo": 1}``
  2. Code-fenced JSON — `````json\\n{...}\\n`````
  3. Prose-wrapped JSON — ``Here is the plan: {...}. Hope this helps!``

The architect previously inlined this; extracting it here lets the new
``development.layers.*`` generators share one well-tested parser. This
module is a behavior-preserving refactor of architect's ``_try_parse_json``.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Strips a leading `````json`` (or bare ```````)
# fence and a trailing ``````` fence. Multiline so it matches
# the fences whether or not they sit on their own line.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_llm_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM response.

    Tries, in order:
      1. Direct ``json.loads`` of the trimmed text.
      2. Strip `````json`` / ``````` fences, retry.
      3. Slice from first ``{`` to last ``}`` and retry.

    Returns the parsed dict on success, or ``None`` if every strategy
    fails. Non-dict top-level JSON (lists, scalars) also returns ``None``
    — every caller in this codebase expects an object.
    """
    if not text:
        return None
    raw = text.strip()

    # 1. Direct parse.
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        pass

    # 2. Strip code fences and retry.
    stripped = _FENCE_RE.sub("", raw).strip()
    if stripped != raw:
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            pass

    # 3. First ``{`` to last ``}`` substring.
    first = raw.find("{")
    last = raw.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = raw[first : last + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            pass

    return None
