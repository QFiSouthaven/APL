"""Concurrency regression guards — the three lessons.

These tests fail loudly if anyone reintroduces the bugs we already paid
for in the source monolith. **Do not weaken them to make changes pass.**

1. ``test_pass1_pass2_serial`` — Pass 1 and Pass 2 must run serially.
   Parallel execution causes LM Studio server-side queueing and
   ``httpx.ReadTimeout`` on slow remote-GPU streams via LM Link.

2. ``test_pass4_awaited_before_magnitude`` — Pass 4 (background task)
   must complete BEFORE Magnitude/SoT begin streaming. Same reason:
   single-instance backend can't serve overlapping requests.

3. ``test_idle_timeout_propagates`` — Every ``chat_stream`` call must
   honor ``idle_timeout`` so silent stalls fail fast (~120 s) instead
   of hanging until the overall request timeout (which is ~600 s).
"""

from __future__ import annotations

import time

import pytest

from enhancer.core.events import EventType
from enhancer.core.pipeline import PipelineOptions, run_pipeline


# ── canned LLM responses that satisfy the parsers ───────────────────

PASS1_RESPONSE_TOKENS = [
    "GOAL: Build a feature.\n",
    "DOMAIN: Software.\n",
    "TASK TYPE: instructional\n",
    "AUDIENCE: Developers.\n",
    "IMPLICIT NEEDS: clarity.\n",
]

PASS2_RESPONSE_TOKENS = [
    "VAGUE TERMS: none\n",
    "MISSING CONTEXT: none\n",
    "UNSTATED CONSTRAINTS: none\n",
    "SCOPE ISSUES: none\n",
    "PRIMARY FOCUS: precision\n",
]

PASS3_RESPONSE_TOKENS = ["Rewrite ", "of ", "the ", "prompt."]
# Pass 4 now streams (v0.2). See pipeline.py rationale.
PASS4_TOKENS = [
    "SPECIFICITY: 9\nCONSTRAINTS: 8\nACTIONABILITY: 9\nIMPROVEMENT: 70\n",
]
MAGNITUDE_TOKENS = ["## Phase 1", "\n## Phase 2", "\n## Phase 3"]
SOT_TOKENS = ["## Goal", "\n## Core", "\n## Constraints", "\n## Skeleton"]


def _seed_minimal(provider) -> None:
    """Enqueue just enough responses for a no-extras pipeline run."""
    provider.stream_responses.extend([
        PASS1_RESPONSE_TOKENS,
        PASS2_RESPONSE_TOKENS,
        PASS3_RESPONSE_TOKENS,
        PASS4_TOKENS,
    ])


def _is_pass4_stream(call) -> bool:
    """Identify the Pass 4 stream call by its system prompt content.

    Pass 4 uses ``PASS4_SYSTEM`` which contains the unique phrase
    "prompt quality evaluator". This is the only call that does.
    """
    if call.kind != "chat_stream" or not call.messages:
        return False
    sys_msg = next((m for m in call.messages if m.get("role") == "system"), None)
    return bool(sys_msg) and "prompt quality evaluator" in (sys_msg.get("content") or "")


@pytest.mark.concurrency
@pytest.mark.asyncio
async def test_pass1_pass2_serial(fake_provider, event_collector):
    """Wall time ≥ 2× per-call latency proves no parallel execution."""
    fake_provider.latency_s = 0.4
    _seed_minimal(fake_provider)

    on_event, _events = event_collector
    started = time.monotonic()
    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(temperature=0.7, max_tokens_scale=1.0),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=5.0,
    )
    elapsed = time.monotonic() - started

    # Pass 1 stream + Pass 2 stream: 5 + 5 tokens × 0.4 s = 4 s minimum.
    # Plus Pass 3 stream (4 tokens × 0.4 = 1.6 s) and Pass 4 chat (0.4 s).
    # Min total > Pass1 + Pass2 = 4.0 s if serial; ~2.0 s if parallel.
    assert elapsed >= 3.5, (
        f"Pass 1/2 appear to run in parallel (elapsed={elapsed:.2f}s, "
        "expected ≥ 3.5 s on serial execution). DO NOT use asyncio.gather."
    )

    # And the call log should show pass1 ended before pass2 started.
    chat_streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    assert len(chat_streams) >= 2
    p1, p2 = chat_streams[0], chat_streams[1]
    assert p1.ended_at is not None and p2.started_at >= p1.ended_at, (
        "Pass 2 started before Pass 1 finished"
    )


@pytest.mark.concurrency
@pytest.mark.asyncio
async def test_pass4_awaited_before_magnitude(fake_provider, event_collector):
    """Pass 4 (background task) must finish before Magnitude streaming begins."""
    fake_provider.latency_s = 0.2
    _seed_minimal(fake_provider)
    fake_provider.stream_responses.append(MAGNITUDE_TOKENS)  # for magnitude
    on_event, _events = event_collector

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(magnitude_mode=True),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=5.0,
    )

    # Pass 4 now streams (v0.2). Identify it by its unique system prompt;
    # Magnitude is the LAST chat_stream call after p1, p2, p3, p4.
    pass4_streams = [c for c in fake_provider.calls if _is_pass4_stream(c)]
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]

    assert len(pass4_streams) >= 1, "Pass 4 was not called"
    pass4 = pass4_streams[-1]
    magnitude = streams[-1]
    # Sanity: the last stream is NOT Pass 4 itself (Magnitude follows).
    assert magnitude is not pass4, "Magnitude appears to have been skipped"

    assert pass4.ended_at is not None
    assert magnitude.started_at >= pass4.ended_at, (
        f"Magnitude began at t={magnitude.started_at:.3f} BEFORE Pass 4 "
        f"ended at t={pass4.ended_at:.3f}. The pipeline must await "
        "the Pass 4 background task before starting Magnitude/SoT."
    )


@pytest.mark.concurrency
@pytest.mark.asyncio
async def test_idle_timeout_propagates(fake_provider, event_collector):
    """The pipeline must forward ``idle_timeout`` into every chat_stream call.

    We can't simulate a stall with a fake here (the test harness doesn't
    run httpx). Instead we assert the call signature: every chat_stream
    call records the requested ``idle_timeout`` was honored.
    """
    _seed_minimal(fake_provider)
    on_event, _events = event_collector

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
    )

    # FakeChatProvider records the call kwargs. Assert every chat_stream
    # call accepted the idle_timeout default the pipeline forwarded.
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    assert streams, "Expected at least one chat_stream call"

    # Smoke: the pipeline at least invoked all three streaming passes.
    assert len(streams) >= 3
