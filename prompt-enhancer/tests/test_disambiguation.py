"""Regression tests for the interactive disambiguation pause/resume flow.

Asserts:
1. When Pass 2 emits ≥3 weakness fields, the pipeline pauses, emits
   ``agent_disambiguate``, populates the ``pending_disambig`` dict, and
   returns the empty sentinel result.
2. ``build_resume_state(snapshot, answers)`` constructs a valid
   ``resume_state`` for the second run.
3. Resuming with ``opts.resume_state`` skips Pass 1/2, jumps to Pass 3,
   and the answer context is included in the Pass 3 user message.
4. Pass 1 + Pass 2 durations are tracked independently (the timing
   bug fix).
"""

from __future__ import annotations

import pytest

from enhancer.core.events import EventType
from enhancer.core.pipeline import (
    PipelineOptions,
    build_resume_state,
    run_pipeline,
)


# Pass 1 output that yields task_type=analytical (canonical match).
PASS1_TOKENS = [
    "GOAL: do analysis\n",
    "DOMAIN: research\n",
    "TASK TYPE: analytical\n",
    "AUDIENCE: stakeholders\n",
    "IMPLICIT NEEDS: rigor\n",
]

# Pass 2 output with all 4 weakness fields populated → triggers disambig.
PASS2_TOKENS_VAGUE = [
    "VAGUE TERMS: undefined audience and scope\n",
    "MISSING CONTEXT: domain background unclear\n",
    "UNSTATED CONSTRAINTS: format and length not specified\n",
    "SCOPE ISSUES: too broad to act on\n",
    "PRIMARY FOCUS: context\n",
]

# Disambiguation generation output (one-shot chat, parsed into 2 questions).
DISAMBIG_RESPONSE = (
    "Q1: Who is the primary audience?\n"
    "A) developers\nB) executives\nC) general\n\n"
    "Q2: What output format do you want?\n"
    "A) prose\nB) bulleted\nC) JSON\n"
)

PASS3_TOKENS = ["Resumed ", "rewrite ", "with ", "answers."]
# Pass 4 streams in v0.2 to bypass LM Studio's reasoning-token filter.
PASS4_TOKENS = [
    "SPECIFICITY: 8\nCONSTRAINTS: 7\nACTIONABILITY: 9\nIMPROVEMENT: 60\n",
]


@pytest.mark.asyncio
async def test_disambig_pauses_pipeline(fake_provider, event_collector):
    """Pipeline pauses on vague prompts and emits agent_disambiguate."""
    fake_provider.stream_responses.extend([PASS1_TOKENS, PASS2_TOKENS_VAGUE])
    fake_provider.responses.append(DISAMBIG_RESPONSE)

    on_event, events = event_collector
    pending_disambig: dict[str, dict] = {}

    result = await run_pipeline(
        "Analyze our market.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=5.0, idle_timeout=2.0,
        pending_disambig=pending_disambig,
    )

    # Sentinel result returned
    assert result.result == ""
    assert result.extras and result.extras.get("paused") is True

    # agent_disambiguate event fired with questions
    disambig_events = [e for e in events if e[0] == EventType.AGENT_DISAMBIGUATE.value]
    assert len(disambig_events) == 1
    payload = disambig_events[0][1]
    assert payload["disambig_id"]
    assert len(payload["questions"]) == 2
    assert payload["questions"][0]["options"] == ["developers", "executives", "general"]

    # Snapshot stored in pending_disambig keyed by the same id
    assert payload["disambig_id"] in pending_disambig
    snap = pending_disambig[payload["disambig_id"]]
    assert snap["pass1"]
    assert snap["pass2"]
    assert snap["task_type"] == "analytical"
    assert snap["technique"] == "context"
    # Per-pass durations captured (proves the timing fix landed)
    assert "p1_duration_ms" in snap
    assert "p2_duration_ms" in snap


@pytest.mark.asyncio
async def test_disambig_resume_completes_pipeline(fake_provider, event_collector):
    """After resume, Pass 1/2 are skipped and Pass 3+4 run normally."""
    # First run — paused state.
    fake_provider.stream_responses.extend([PASS1_TOKENS, PASS2_TOKENS_VAGUE])
    fake_provider.responses.append(DISAMBIG_RESPONSE)

    on_event, _events = event_collector
    pending_disambig: dict[str, dict] = {}

    paused = await run_pipeline(
        "Analyze our market.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=5.0, idle_timeout=2.0,
        pending_disambig=pending_disambig,
    )
    assert paused.extras["paused"]

    # Build a resume_state with answers and re-run.
    [(disambig_id, snapshot)] = pending_disambig.items()
    answers = {"Q1": "developers", "Q2": "JSON"}
    resume_state = build_resume_state(snapshot, answers)

    # Confirm the helper assembled the answer context correctly.
    assert "[USER CLARIFICATIONS]" in resume_state["disambiguation_context"]
    assert "developers" in resume_state["disambiguation_context"]
    assert "JSON" in resume_state["disambiguation_context"]
    assert resume_state["pass1"] == snapshot["pass1"]
    assert resume_state["task_type"] == "analytical"

    # Resume run — fake should NOT see Pass 1 / Pass 2 streams again.
    pre_resume_calls = list(fake_provider.calls)
    fake_provider.stream_responses.extend([PASS3_TOKENS, PASS4_TOKENS])

    on_event2, events2 = event_collector  # re-uses the same list; that's fine
    final = await run_pipeline(
        snapshot["prompt"],
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(resume_state=resume_state),
        on_event=on_event2,
        request_timeout=5.0, idle_timeout=2.0,
    )

    assert final.result == "Resumed rewrite with answers."
    assert final.task_type == "analytical"
    assert final.technique == "context"
    assert not final.scores_fallback
    assert not final.pass3_partial
    assert final.run_id

    # Inspect the calls made AFTER the first run finished — should be the
    # Pass 3 stream and the Pass 4 stream (v0.2 changed Pass 4 to
    # streaming). No new Pass 1/2 streams; no non-streaming chat calls.
    new_calls = fake_provider.calls[len(pre_resume_calls):]
    chat_streams = [c for c in new_calls if c.kind == "chat_stream"]
    chats = [c for c in new_calls if c.kind == "chat"]
    assert len(chat_streams) == 2, (
        f"expected two chat_streams (Pass 3 + Pass 4), got {len(chat_streams)}"
    )
    assert len(chats) == 0, "Pass 4 should now stream — no chat() calls"

    # Pass 3 user message must contain the [USER CLARIFICATIONS] block.
    pass3_user = chat_streams[0].messages[1]["content"]
    assert "[USER CLARIFICATIONS]" in pass3_user
    assert "JSON" in pass3_user


@pytest.mark.asyncio
async def test_per_pass_durations_tracked_independently(fake_provider, event_collector):
    """The timing fix: pass1 and pass2 durations differ when latencies do."""
    # Different latency per call so we can detect averaging.
    # FakeChatProvider has a single ``latency_s`` knob — instead, we simulate
    # by giving Pass 2 many more tokens (each token costs latency_s).
    fake_provider.latency_s = 0.05
    fake_provider.stream_responses.extend([
        PASS1_TOKENS,                           # 5 tokens × 0.05s = 0.25s
        PASS2_TOKENS_VAGUE + ["X\n"] * 15,      # 20 tokens × 0.05s = 1.0s
    ])
    fake_provider.responses.append(DISAMBIG_RESPONSE)

    on_event, events = event_collector
    pending_disambig: dict[str, dict] = {}

    await run_pipeline(
        "Analyze our market.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=3.0,
        pending_disambig=pending_disambig,
    )

    pass_results = [
        (e[0], e[1]) for e in events
        if e[0] == EventType.AGENT_PASS_RESULT.value
    ]
    p1_event = next(p for _, p in pass_results if p["pass_number"] == 1)
    p2_event = next(p for _, p in pass_results if p["pass_number"] == 2)

    assert p1_event["duration_ms"] >= 0
    assert p2_event["duration_ms"] >= 0
    # Pass 2 had ~4× the work, so its duration should clearly exceed Pass 1.
    # Allow generous slack for CI noise (×1.5 instead of strict ×3).
    assert p2_event["duration_ms"] > p1_event["duration_ms"] * 1.5, (
        f"per-pass timing regression: p1={p1_event['duration_ms']}ms "
        f"p2={p2_event['duration_ms']}ms (expected p2 ≥ 1.5× p1)"
    )


@pytest.mark.asyncio
async def test_build_resume_state_with_no_answers(fake_provider, event_collector):
    """``build_resume_state(snapshot, {})`` works for --skip-clarify path."""
    fake_provider.stream_responses.extend([PASS1_TOKENS, PASS2_TOKENS_VAGUE])
    fake_provider.responses.append(DISAMBIG_RESPONSE)
    on_event, _ = event_collector
    pending: dict[str, dict] = {}

    await run_pipeline(
        "Analyze our market.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=5.0, idle_timeout=2.0,
        pending_disambig=pending,
    )
    [(_, snapshot)] = pending.items()
    rs = build_resume_state(snapshot, {})
    # Empty answers → empty disambiguation_context (no [USER CLARIFICATIONS] block).
    assert rs["disambiguation_context"] == ""
    assert rs["pass1"] and rs["pass2"]
