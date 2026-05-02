"""Context-budget detection and smart truncation.

Lifted from ``swarm-agent-dev/src/webui/mods/agent_pipeline.py`` lines
27-128 (context detection) and 131-145 (truncation). Both behaviors are
preserved verbatim because they shape per-pass output length — and the
analytics dashboard filters by output length.

Per-pass token budgets (the formulas at agent_pipeline.py:920-927) live
in :func:`compute_pass_budgets` so the standalone reproduces output
behavior identically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

# Regex patterns for context-size hints in model names.
_CTX_PATTERNS = [
    re.compile(r"(\d+)[kK][-_]?ctx", re.I),         # "32k-ctx", "8K_ctx"
    re.compile(r"ctx[-_]?(\d+)[kK]", re.I),          # "ctx-32k", "ctx32K"
    re.compile(r"[-_.](\d+)[kK](?:[-_.]|$)", re.I),  # "-32k-", ".8k."
]

# Default per-pass char budget when context can't be detected (~3000 tokens).
DEFAULT_CHAR_BUDGET = 12_000


def _query_model_context_length(model_name: str, management_url: str) -> int | None:
    """Query LM Studio's management API for the model's loaded context length.

    Returns context length in tokens, or ``None`` when unavailable.
    """
    if not model_name:
        return None
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{management_url}/api/v0/models")
            r.raise_for_status()
        for m in r.json().get("data", []):
            if m.get("id") == model_name:
                loaded = m.get("loaded_context_length")
                if loaded and loaded > 0:
                    return loaded
                return m.get("max_context_length")
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    return None


def detect_context_budget(model_name: str, management_url: str) -> int:
    """Derive a per-pass char budget from the model's context window.

    1. Query LM Studio mgmt API for the loaded context length.
    2. Fall back to regex parsing of the model name.
    3. Fall back to param-count heuristics.
    4. Reserve 25% for system prompts + generation headroom.
    5. Convert tokens → chars (×4).
    """
    if not model_name:
        return DEFAULT_CHAR_BUDGET

    api_ctx = _query_model_context_length(model_name, management_url)
    if api_ctx and api_ctx > 0:
        usable = int(api_ctx * 0.75)
        return usable * 4

    for pat in _CTX_PATTERNS:
        m = pat.search(model_name)
        if m:
            ctx_k = int(m.group(1))
            ctx_tokens = ctx_k * 1024
            usable_tokens = int(ctx_tokens * 0.75)
            return usable_tokens * 4

    name_lower = model_name.lower()
    if any(s in name_lower for s in ("1b", "2b", "3b", "4b")):
        return 3072 * 4
    if any(s in name_lower for s in ("7b", "8b", "9b")):
        return 6144 * 4
    if any(s in name_lower for s in ("12b", "13b", "14b")):
        return 12288 * 4
    if any(s in name_lower for s in (
        "22b", "24b", "27b", "32b", "34b",
        "70b", "72b",
    )):
        return 24576 * 4

    return DEFAULT_CHAR_BUDGET


def truncate(text: str, max_chars: int, label: str = "") -> str:
    """Smart-truncate: keep first 20% + last 80% so tail instructions survive.

    Marker size is dynamic; if usable < 40 chars after the marker, hard-chop.
    """
    if len(text) <= max_chars:
        return text
    marker = (
        f"\n[...truncated {label} middle, "
        f"{len(text)} chars → {max_chars}]\n"
    )
    usable = max_chars - len(marker)
    if usable < 40:
        return text[:max_chars]
    head = usable // 5       # 20%
    tail = usable - head     # 80%
    return text[:head] + marker + text[-tail:]


@dataclass(frozen=True)
class PassBudgets:
    """Token budgets per pass, derived from total context budget (chars)."""

    analysis: int
    rewrite: int
    score: int
    persona: int
    magnitude: int
    sot: int


def compute_pass_budgets(char_budget: int) -> PassBudgets:
    """Per-pass token budgets — formulas from agent_pipeline.py:920-927.

    ``char_budget`` is in chars; we convert at ~4 chars/token.

    The ``score`` budget was bumped from 200 → 400 in v0.2 because
    reasoning-token models (notably gpt-oss family) consume their first
    100-200 tokens on internal thinking before emitting visible content.
    See ``.claude/knowledge/lm-studio-models.md`` §1.
    """
    tok = char_budget // 4
    return PassBudgets(
        analysis=max(tok // 8, 512),
        rewrite=max(tok // 2, 2048),
        score=400,
        persona=400,
        magnitude=max(tok // 2, 4096),
        sot=max(tok // 3, 2048),
    )


def scaled(n: int, max_tokens_scale: float) -> int:
    """Apply user's max-tokens slider to a per-pass budget.

    Mirrors ``_scale`` inside ``_run_pipeline``: never below 1.
    """
    return max(1, int(n * max_tokens_scale))
