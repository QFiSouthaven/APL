"""Tests for the multi-LLM code review pipeline.

Covers the three-call dialogue (Agent A → Agent B → Consensus) and its
fallback behavior on parse failures, plus the prompt-byte invariant.

A FakeLMClient (defined locally; we don't import development's) returns
canned responses indexed by call number, so we can drive A/B/Consensus
through every consensus-aggregation rule.
"""
from __future__ import annotations

import json

import pytest

from round_robin import code_review
from round_robin.code_review import (
    AGENT_A_SYSTEM,
    AGENT_B_SYSTEM,
    CONSENSUS_SYSTEM,
    review_with_dialogue,
)


# ── Local FakeLMClient ─────────────────────────────────────────────────


class FakeLMClient:
    """Minimal stand-in for LMLinkClient. Records every chat() call.

    Pass `responses` as a list of strings; calls are answered in order. If
    a response is an Exception instance, it's raised instead of returned —
    handy for the "LM Studio raises mid-dialogue" test.
    """

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        model: str = "",
        temperature: float = 0.7,
        max_tokens=None,
        response_format=None,
    ) -> str:
        self.calls.append({
            "messages": messages,
            "model": model,
            "temperature": temperature,
        })
        if not self.responses:
            raise RuntimeError("FakeLMClient ran out of canned responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _verdict_json(approved: bool, issues=None, request_regenerate: bool = False,
                  summary: str = "ok") -> str:
    return json.dumps({
        "approved": approved,
        "issues": issues or [],
        "request_regenerate": request_regenerate,
        "summary": summary,
    })


def _consensus_json(approved: bool, issues=None, request_regenerate: bool = False,
                    consensus: str = "synth") -> str:
    return json.dumps({
        "approved": approved,
        "issues": issues or [],
        "request_regenerate": request_regenerate,
        "consensus": consensus,
    })


# ── Happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_both_approve_consensus_approved():
    """A approves + B approves → consensus approved=true, regen=false."""
    client = FakeLMClient([
        _verdict_json(True, summary="A: looks good"),
        _verdict_json(True, summary="B: edge cases handled"),
        _consensus_json(True, consensus="both engineers approved cleanly"),
    ])
    out = await review_with_dialogue(
        "core", "do core things", {"a.py": "x = 1"}, lm_client=client,
    )
    assert out["approved"] is True
    assert out["request_regenerate"] is False
    assert out["issues"] == []
    assert "agents" in out
    assert "agent_a_verdict" in out["agents"]
    assert "agent_b_verdict" in out["agents"]
    assert "consensus" in out["agents"]
    # Exactly 3 LM calls.
    assert len(client.calls) == 3


# ── Aggregation rules ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rejects_b_approves_consensus_rejected():
    """approved IFF both approved — A rejects → consensus rejected."""
    client = FakeLMClient([
        _verdict_json(False, issues=["bug on line 3"], summary="A: needs fix"),
        _verdict_json(True, summary="B: looks fine"),
        # Consensus LLM may say approved=true, but our deterministic rule
        # overrides: A rejected, so the unified verdict is rejected.
        _consensus_json(True, consensus="reconciliation"),
    ])
    out = await review_with_dialogue(
        "x", "purpose", {"f.py": "..."}, lm_client=client,
    )
    assert out["approved"] is False, "any-rejection-rejects rule"


@pytest.mark.asyncio
async def test_b_rejects_with_regenerate_consensus_regenerate_true():
    """A approves + B rejects with regen → consensus request_regenerate=true."""
    client = FakeLMClient([
        _verdict_json(True, summary="A: ok"),
        _verdict_json(False, issues=["off-by-one"], request_regenerate=True,
                      summary="B: regenerate"),
        _consensus_json(False, issues=["off-by-one"], request_regenerate=False,
                         consensus="B wants regen"),
    ])
    out = await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)
    assert out["approved"] is False
    assert out["request_regenerate"] is True


@pytest.mark.asyncio
async def test_both_reject_both_request_regen():
    """Both reject + both regen → unified rejected + regen=true."""
    client = FakeLMClient([
        _verdict_json(False, issues=["i1"], request_regenerate=True),
        _verdict_json(False, issues=["i2"], request_regenerate=True),
        _consensus_json(False, issues=["i1", "i2"], request_regenerate=True,
                         consensus="both want regen"),
    ])
    out = await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)
    assert out["approved"] is False
    assert out["request_regenerate"] is True


@pytest.mark.asyncio
async def test_distinct_issues_aggregate_no_duplicates():
    """A's and B's issues fold into the consensus list with case-insensitive dedup."""
    client = FakeLMClient([
        _verdict_json(False, issues=["Race condition in init", "Missing docstring"]),
        # Note: "race condition in init" is a case-variant of A's first issue.
        _verdict_json(False, issues=["race condition in init", "No retry logic"]),
        # Consensus LLM repeats one of them — should still de-dupe.
        _consensus_json(False, issues=["Missing docstring", "Bad logging"]),
    ])
    out = await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)
    # Order: A's first-seen, then B's new ones, then consensus's new ones.
    # 4 distinct: race condition / missing docstring / no retry / bad logging.
    issues_lower = [i.lower() for i in out["issues"]]
    assert "race condition in init" in issues_lower
    assert "missing docstring" in issues_lower
    assert "no retry logic" in issues_lower
    assert "bad logging" in issues_lower
    # No dups (case-insensitive).
    assert len(issues_lower) == len(set(issues_lower))


# ── Fallback behavior on parse failures ────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_json_from_agent_a_fallback_no_crash():
    """Garbage from A → A's verdict defaults to approve; pipeline continues."""
    client = FakeLMClient([
        "this is not json at all, just prose",  # Agent A garbage
        _verdict_json(True, summary="B: ok"),
        _consensus_json(True, consensus="default-A + B = ok"),
    ])
    out = await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)
    # A defaulted to approved, B approved → unified approved.
    assert out["approved"] is True
    # No crash, all 3 calls fired.
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_garbage_json_from_consensus_falls_back():
    """Consensus parse failure → conservative approved=true fallback verdict."""
    client = FakeLMClient([
        _verdict_json(True),
        _verdict_json(True),
        "garbage from the consensus pass",
    ])
    out = await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)
    # Fallback verdict: approved=true, no issues, no regen.
    assert out["approved"] is True
    assert out["issues"] == []
    assert out["request_regenerate"] is False
    assert "review failed" in out["agents"]["consensus"]


# ── Transport failure propagation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_lm_studio_raises_propagates_clean_exception():
    """If LMLinkClient.chat raises, review_with_dialogue propagates it.

    The /api/review handler turns this into a 503 — separate test.
    """
    from round_robin.lm_client import LMLinkError

    client = FakeLMClient([
        _verdict_json(True),
        LMLinkError("LM Studio: model not loaded"),
        # Third response is never reached.
    ])
    with pytest.raises(LMLinkError):
        await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)


# ── Edge cases ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_files_dict_still_produces_verdict():
    """Empty files dict → review still runs; vacuously approved by default."""
    client = FakeLMClient([
        _verdict_json(True, summary="A: nothing to review"),
        _verdict_json(True, summary="B: vacuous"),
        _consensus_json(True, consensus="vacuously approved"),
    ])
    out = await review_with_dialogue("x", "p", {}, lm_client=client)
    assert out["approved"] is True
    assert "issues" in out and isinstance(out["issues"], list)


# ── Prompt invariants ──────────────────────────────────────────────────


def test_system_prompts_byte_match_pinned_constants():
    """The pinned system prompts must not drift silently. If you intend to
    bump them, update this test in the same change so reviewers see it."""
    assert AGENT_A_SYSTEM == (
        "You are a pragmatic senior engineer reviewing one layer of a code "
        "build. Focus on correctness for the stated purpose, readability, "
        "and obvious bugs. Output JSON ONLY: "
        '{"approved": bool, "issues": ["..."], "request_regenerate": bool, '
        '"summary": "1-line takeaway"}. '
        "Be direct. Output only the JSON, no prose."
    )
    assert AGENT_B_SYSTEM == (
        "You are a rigorous code critic. Another engineer has already "
        "reviewed the same code; their verdict is included below for "
        "context. Focus on edge cases, race conditions, error handling, "
        "and anything they missed. Output JSON ONLY in the same shape: "
        '{"approved": bool, "issues": ["..."], "request_regenerate": bool, '
        '"summary": "1-line takeaway"}. '
        "Output only the JSON, no prose."
    )
    assert CONSENSUS_SYSTEM == (
        "You are a tech lead reconciling two engineers' code reviews. "
        "Given both verdicts, produce a unified verdict. The build is "
        "approved IFF both engineers approved. The build needs "
        "regeneration IFF at least one engineer flagged a mechanical bug "
        "AND requested regenerate. Aggregate distinct issues. Output JSON "
        'ONLY: {"approved": bool, "issues": ["..."], "request_regenerate": bool, '
        '"consensus": "1-2 line synthesis"}. '
        "No prose outside the JSON."
    )


@pytest.mark.asyncio
async def test_system_prompts_actually_sent_in_messages():
    """Sanity: each agent call's first message should carry its pinned system prompt."""
    client = FakeLMClient([
        _verdict_json(True),
        _verdict_json(True),
        _consensus_json(True),
    ])
    await review_with_dialogue("x", "p", {"f.py": "."}, lm_client=client)
    assert client.calls[0]["messages"][0]["content"] == AGENT_A_SYSTEM
    assert client.calls[1]["messages"][0]["content"] == AGENT_B_SYSTEM
    assert client.calls[2]["messages"][0]["content"] == CONSENSUS_SYSTEM


# ── JSON parser unit tests ─────────────────────────────────────────────


def test_parse_json_handles_clean_fenced_and_embedded():
    """3-stage parser: clean / fenced / embedded."""
    parse = code_review._parse_json
    assert parse('{"approved": true}') == {"approved": True}
    assert parse('```json\n{"approved": false}\n```') == {"approved": False}
    assert parse('Here is my answer: {"approved": true} thanks.') == {"approved": True}
    assert parse('not json at all') is None
    assert parse('') is None
