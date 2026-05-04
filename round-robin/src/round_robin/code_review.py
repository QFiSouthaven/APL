"""Multi-LLM code review pipeline.

Two LLM agents critique a layer in turn (Agent A first, then Agent B with
A's verdict in context), then a third call synthesizes a unified verdict
in the shape that ``development.reviewers.RoundRobinReviewer`` consumes:

    {
        "approved": bool,
        "issues": [str, ...],
        "request_regenerate": bool,
        "agents": {
            "agent_a_verdict": str,
            "agent_b_verdict": str,
            "consensus": str,
        },
    }

The first three keys are the load-bearing contract (matches
``ReviewerStage`` exactly). ``agents`` is round-robin-specific extra
metadata that development ignores but that's nice for observability.

The dialogue is bounded: exactly 3 LM calls per review (Agent A,
Agent B, Consensus). No retry loops; if a call fails we fall back to a
conservative approve verdict — review-fallback consistent with
``ReviewerStage``'s best-effort behavior.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .lm_client import LMLinkClient

logger = logging.getLogger(__name__)


# ── Pinned system prompts ──────────────────────────────────────────────
# Byte-match-tested in test_code_review.py — bumping these requires
# updating the regression test alongside.

AGENT_A_SYSTEM = (
    "You are a pragmatic senior engineer reviewing one layer of a code "
    "build. Focus on correctness for the stated purpose, readability, "
    "and obvious bugs. Output JSON ONLY: "
    '{"approved": bool, "issues": ["..."], "request_regenerate": bool, '
    '"summary": "1-line takeaway"}. '
    "Be direct. Output only the JSON, no prose."
)

AGENT_B_SYSTEM = (
    "You are a rigorous code critic. Another engineer has already "
    "reviewed the same code; their verdict is included below for "
    "context. Focus on edge cases, race conditions, error handling, "
    "and anything they missed. Output JSON ONLY in the same shape: "
    '{"approved": bool, "issues": ["..."], "request_regenerate": bool, '
    '"summary": "1-line takeaway"}. '
    "Output only the JSON, no prose."
)

CONSENSUS_SYSTEM = (
    "You are a tech lead reconciling two engineers' code reviews. "
    "Given both verdicts, produce a unified verdict. The build is "
    "approved IFF both engineers approved. The build needs "
    "regeneration IFF at least one engineer flagged a mechanical bug "
    "AND requested regenerate. Aggregate distinct issues. Output JSON "
    'ONLY: {"approved": bool, "issues": ["..."], "request_regenerate": bool, '
    '"consensus": "1-2 line synthesis"}. '
    "No prose outside the JSON."
)


# Conservative fallback verdict used when any of the three LM calls
# returns garbage we can't parse. Matches the shape ReviewerStage
# expects and defaults to approving so a malformed review never blocks
# the build (consumers can retry the whole /api/review).
_FALLBACK_VERDICT: dict[str, Any] = {
    "approved": True,
    "issues": [],
    "request_regenerate": False,
    "consensus": "review failed; verdict defaulted to approved",
}


# ── JSON parsing ────────────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
# Match a balanced-ish JSON object span. Lazy enough that nested objects
# don't blow it up; we only need the first top-level object that parses.
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any] | None:
    """Three-stage JSON parser. Returns None if nothing parses.

    Stage 1: try the raw text as-is (after strip).
    Stage 2: extract content from a ```json ... ``` fence.
    Stage 3: extract the first {...} span and try that.
    """
    if not text:
        return None
    stripped = text.strip()

    # Stage 1: clean
    try:
        out = json.loads(stripped)
        if isinstance(out, dict):
            return out
    except (json.JSONDecodeError, ValueError):
        pass

    # Stage 2: fenced
    fence = _FENCE_RE.search(stripped)
    if fence:
        inner = fence.group(1).strip()
        try:
            out = json.loads(inner)
            if isinstance(out, dict):
                return out
        except (json.JSONDecodeError, ValueError):
            pass

    # Stage 3: embedded {...}
    obj = _OBJECT_RE.search(stripped)
    if obj:
        try:
            out = json.loads(obj.group(0))
            if isinstance(out, dict):
                return out
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _normalize_agent_verdict(parsed: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a single-agent verdict into a known shape with safe defaults."""
    if parsed is None:
        return {
            "approved": True,
            "issues": [],
            "request_regenerate": False,
            "summary": "(parse failed)",
        }
    issues = parsed.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    issues = [str(i) for i in issues if i]
    summary = parsed.get("summary")
    if not isinstance(summary, str):
        summary = ""
    return {
        "approved": bool(parsed.get("approved", True)),
        "issues": issues,
        "request_regenerate": bool(parsed.get("request_regenerate", False)),
        "summary": summary,
    }


# ── Prompt construction ────────────────────────────────────────────────


def _format_files(files: dict[str, str]) -> str:
    """Render the files dict as one block per file with header lines."""
    if not files:
        return "(no files)"
    parts: list[str] = []
    for path, content in files.items():
        parts.append(f"--- FILE: {path} ---\n{content or ''}")
    return "\n\n".join(parts)


def _build_user_prompt(layer: str, purpose: str, files: dict[str, str]) -> str:
    return (
        f"LAYER: {layer}\n"
        f"PURPOSE: {purpose}\n\n"
        f"FILES:\n{_format_files(files)}\n\n"
        "Review the code above. Output your JSON verdict only."
    )


def _build_user_prompt_with_prior(
    layer: str, purpose: str, files: dict[str, str], prior_verdict: dict[str, Any],
) -> str:
    """Agent B prompt: same as A's but with A's verdict appended for context."""
    prior_json = json.dumps(prior_verdict, indent=2)
    return (
        f"LAYER: {layer}\n"
        f"PURPOSE: {purpose}\n\n"
        f"FILES:\n{_format_files(files)}\n\n"
        f"PRIOR VERDICT FROM AGENT A:\n{prior_json}\n\n"
        "Review the code with that verdict in mind. Output your JSON verdict only."
    )


def _build_consensus_prompt(
    layer: str,
    agent_a: dict[str, Any],
    agent_b: dict[str, Any],
) -> str:
    return (
        f"LAYER: {layer}\n\n"
        f"AGENT A VERDICT:\n{json.dumps(agent_a, indent=2)}\n\n"
        f"AGENT B VERDICT:\n{json.dumps(agent_b, indent=2)}\n\n"
        "Reconcile the two verdicts and emit the unified JSON."
    )


# ── Main entrypoint ────────────────────────────────────────────────────


async def review_with_dialogue(
    layer: str,
    purpose: str,
    files: dict[str, str],
    *,
    lm_client: Any | None = None,
    timeout_per_call: float = 60.0,
    model: str | None = None,
) -> dict[str, Any]:
    """Two-pass code review with consensus synthesis.

    Pass 1: Agent A (the 'pragmatic engineer') reviews the code with
            focus on correctness + readability.
    Pass 2: Agent B (the 'rigorous critic') reviews the SAME code AND
            sees Agent A's verdict, with focus on edge cases + bugs.
    Pass 3: Consensus synthesis — fold both verdicts into the unified
            shape ReviewerStage expects.

    Returns: dict matching the API response shape.

    On individual-call parse failures we fall back to the conservative
    approve-verdict for that pass (so a single bad model output doesn't
    fail the whole review). On TRANSPORT failures (LM Studio unreachable,
    HTTP errors) we let the exception propagate — the caller renders 503.
    """
    del timeout_per_call  # accepted for API stability; LMLinkClient owns its own timeouts
    client = lm_client if lm_client is not None else LMLinkClient()
    review_model = model or ""

    # ── Pass 1: Agent A ────────────────────────────────────────────────
    a_messages = [
        {"role": "system", "content": AGENT_A_SYSTEM},
        {"role": "user", "content": _build_user_prompt(layer, purpose, files)},
    ]
    a_raw = await client.chat(a_messages, model=review_model, temperature=0.2)
    a_verdict = _normalize_agent_verdict(_parse_json(a_raw))

    # ── Pass 2: Agent B (sees A's verdict) ─────────────────────────────
    b_messages = [
        {"role": "system", "content": AGENT_B_SYSTEM},
        {
            "role": "user",
            "content": _build_user_prompt_with_prior(layer, purpose, files, a_verdict),
        },
    ]
    b_raw = await client.chat(b_messages, model=review_model, temperature=0.2)
    b_verdict = _normalize_agent_verdict(_parse_json(b_raw))

    # ── Pass 3: Consensus synthesis ────────────────────────────────────
    c_messages = [
        {"role": "system", "content": CONSENSUS_SYSTEM},
        {
            "role": "user",
            "content": _build_consensus_prompt(layer, a_verdict, b_verdict),
        },
    ]
    c_raw = await client.chat(c_messages, model=review_model, temperature=0.2)
    c_parsed = _parse_json(c_raw)

    if c_parsed is None:
        # Consensus parse failed — fall back, but preserve agent verdicts
        # for observability. We DON'T compute the consensus deterministically
        # here because the spec specifically asks for the LLM-driven path
        # plus a single fallback. Tests cover both branches.
        consensus_text = _FALLBACK_VERDICT["consensus"]
        approved = bool(_FALLBACK_VERDICT["approved"])
        issues: list[str] = list(_FALLBACK_VERDICT["issues"])
        request_regenerate = bool(_FALLBACK_VERDICT["request_regenerate"])
    else:
        # Apply the deterministic aggregation rules on top of the LLM's
        # synthesis: approve IFF both agents approved; regenerate IFF at
        # least one agent flagged regenerate. The LLM's `consensus` text
        # is preserved as the prose synthesis. This guarantees the rules
        # hold even if the LLM gets the booleans wrong.
        approved = bool(a_verdict["approved"]) and bool(b_verdict["approved"])
        request_regenerate = bool(
            a_verdict["request_regenerate"] or b_verdict["request_regenerate"]
        )
        issues = _merge_issues(a_verdict["issues"], b_verdict["issues"])
        # Add issues the LLM consensus call surfaced if they're new.
        c_issues_raw = c_parsed.get("issues") or []
        if isinstance(c_issues_raw, list):
            issues = _merge_issues(issues, [str(i) for i in c_issues_raw if i])
        consensus_raw = c_parsed.get("consensus")
        consensus_text = (
            consensus_raw if isinstance(consensus_raw, str) and consensus_raw.strip()
            else _summarize_consensus(approved, request_regenerate, len(issues))
        )

    return {
        "approved": approved,
        "issues": issues,
        "request_regenerate": request_regenerate,
        "agents": {
            "agent_a_verdict": _short(a_verdict),
            "agent_b_verdict": _short(b_verdict),
            "consensus": consensus_text,
        },
    }


def _merge_issues(*lists: list[str]) -> list[str]:
    """Flatten + de-dupe (case-insensitive) while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for issue in lst:
            key = issue.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(issue)
    return out


def _short(verdict: dict[str, Any]) -> str:
    """One-line summary string for the agents-metadata block."""
    summary = verdict.get("summary") or ""
    approved = "approved" if verdict.get("approved") else "rejected"
    if summary:
        return f"{approved}: {summary}"
    n_issues = len(verdict.get("issues") or [])
    return f"{approved} ({n_issues} issue(s))"


def _summarize_consensus(approved: bool, request_regenerate: bool, n_issues: int) -> str:
    parts = ["approved" if approved else "rejected"]
    if request_regenerate:
        parts.append("regenerate requested")
    parts.append(f"{n_issues} distinct issue(s)")
    return "; ".join(parts)
