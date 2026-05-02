"""Regression test for ``_run_with_auto_resume`` (cli/extras.py).

This helper auto-resumes the pipeline when ``compare`` or ``batch``
trigger the disambiguation pause (the bug surfaced live: both A and B
returned P4_DEFAULTS because the pause swallowed the real result).

Asserts:
1. On a vague prompt that triggers disambig, the helper returns a
   completed PipelineResult with real scores (not P4_DEFAULTS).
2. The pipeline runs twice — once paused, once resumed — and the
   second call carries ``opts.resume_state``.
3. On a clear prompt that doesn't trigger disambig, the helper passes
   through cleanly without a second invocation.
"""

from __future__ import annotations

import pytest

from enhancer.cli.extras import _run_with_auto_resume
from enhancer.core.events import EventType, P4_DEFAULTS
from enhancer.core.pipeline import PipelineOptions


# Pass 1 + canonical task type
PASS1_TOKENS = [
    "GOAL: build a thing\n",
    "DOMAIN: software\n",
    "TASK TYPE: instructional\n",
    "AUDIENCE: developers\n",
    "IMPLICIT NEEDS: clarity\n",
]

# Pass 2 with all 4 weakness fields populated → triggers disambig
PASS2_VAGUE = [
    "VAGUE TERMS: undefined scope\n",
    "MISSING CONTEXT: target users unclear\n",
    "UNSTATED CONSTRAINTS: format and length unspecified\n",
    "SCOPE ISSUES: too broad\n",
    "PRIMARY FOCUS: context\n",
]

# Pass 2 with NO non-trivial weakness fields → no disambig
PASS2_CLEAR = [
    "VAGUE TERMS: none\n",
    "MISSING CONTEXT: none\n",
    "UNSTATED CONSTRAINTS: none\n",
    "SCOPE ISSUES: none\n",
    "PRIMARY FOCUS: precision\n",
]

DISAMBIG_RESPONSE = (
    "Q1: Who is the target user?\nA) devs\nB) execs\nC) general\n\n"
    "Q2: What format?\nA) markdown\nB) JSON\nC) prose\n"
)
PASS3_TOKENS = ["Auto-", "resumed ", "rewrite."]
PASS4_TOKENS = [
    "SPECIFICITY: 9\nCONSTRAINTS: 9\nACTIONABILITY: 8\nIMPROVEMENT: 75\n",
]


@pytest.mark.asyncio
async def test_auto_resume_recovers_from_disambig_pause(
    fake_provider, event_collector,
):
    """Vague prompt → pause → auto-resume with empty answers → real scores."""
    # First-run sequence
    fake_provider.stream_responses.extend([PASS1_TOKENS, PASS2_VAGUE])
    fake_provider.responses.append(DISAMBIG_RESPONSE)
    # Resumed-run sequence (Pass 1/2 skipped on resume)
    fake_provider.stream_responses.extend([PASS3_TOKENS, PASS4_TOKENS])

    on_event, events = event_collector

    result = await _run_with_auto_resume(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(temperature=0.7, max_tokens_scale=1.0),
        on_event=on_event,
        request_timeout=5.0, idle_timeout=2.0,
    )

    # Real result, not the empty sentinel
    assert result.result == "Auto-resumed rewrite."
    assert result.task_type in ("instructional", "coding")
    assert not result.scores_fallback, (
        f"compare/batch should auto-resume rather than report fallback; "
        f"scores={result.scores}"
    )
    # Real Pass 4 scores, not the all-5/5/5/50 defaults
    assert result.scores != P4_DEFAULTS
    assert result.scores["improvement"] == 75

    # Disambig event was observed during the pause
    names = [e[0] for e in events]
    assert EventType.AGENT_DISAMBIGUATE.value in names

    # The fake should have seen exactly: 2 streams (P1+P2) + 1 chat (disambig)
    # for the first run, then 2 streams (P3+P4) for the resume = 4 streams + 1 chat.
    chat_streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    chats = [c for c in fake_provider.calls if c.kind == "chat"]
    assert len(chat_streams) == 4, (
        f"expected 4 chat_streams (P1, P2, P3, P4); got {len(chat_streams)}"
    )
    assert len(chats) == 1, (
        f"expected 1 chat (disambig generation); got {len(chats)}"
    )


@pytest.mark.asyncio
async def test_auto_resume_passthrough_when_no_disambig(
    fake_provider, event_collector,
):
    """Clear prompt → no pause → helper just returns the first result."""
    fake_provider.stream_responses.extend([
        PASS1_TOKENS, PASS2_CLEAR, PASS3_TOKENS, PASS4_TOKENS,
    ])

    on_event, events = event_collector

    result = await _run_with_auto_resume(
        "Implement an API endpoint that returns JSON-formatted user data.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=5.0, idle_timeout=2.0,
    )

    assert result.result == "Auto-resumed rewrite."
    assert not result.scores_fallback
    assert result.scores["improvement"] == 75

    # No disambig event because no pause
    names = [e[0] for e in events]
    assert EventType.AGENT_DISAMBIGUATE.value not in names

    # Only the 4 streams; no resume = no extra calls
    chat_streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    chats = [c for c in fake_provider.calls if c.kind == "chat"]
    assert len(chat_streams) == 4
    assert len(chats) == 0
