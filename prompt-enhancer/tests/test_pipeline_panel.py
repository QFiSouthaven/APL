"""ReasoningPanel wiring tests for ``run_pipeline``.

The panel is a strictly-additive, opt-in enhancement. When
``reasoning_panel=None`` (default) the pipeline is byte-identical to v2.0.
When supplied, Pass 1 / Pass 2 / Pass 4 route through ``panel.consult``
and partner outputs land in ``PipelineResult.extras["panel"]``.

Pass 3 (streaming Magnitude/SoT-relevant rewrite) is intentionally NOT
panelled here — its streaming aggregation is a separate problem.

These tests guard four properties:
    * pass-through when no panel: ``provider.chat`` (in panel sense) is
      never called for Pass 1/2/4 — the existing chat_stream path runs.
    * pass-through when panel is supplied: ``panel.consult`` is invoked
      for Pass 1/2/4 and partner providers all receive messages.
    * telemetry: per-pass keys land in ``result.extras["panel"]`` with
      the canonical reviewer.py shape.
    * the serial Pass 1 → Pass 2 invariant survives panel wiring.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from enhancer.core.events import EventType
from enhancer.core.pipeline import PipelineOptions, run_pipeline
from enhancer.llm.reasoning_panel import LLMSlot, ReasoningPanel


# ─── canned responses (must satisfy parsers) ───────────────────────────

_PASS1_TEXT = (
    "GOAL: Build a feature.\n"
    "DOMAIN: Software.\n"
    "TASK TYPE: instructional\n"
    "AUDIENCE: Developers.\n"
    "IMPLICIT NEEDS: clarity.\n"
)
_PASS2_TEXT = (
    "VAGUE TERMS: none\n"
    "MISSING CONTEXT: none\n"
    "UNSTATED CONSTRAINTS: none\n"
    "SCOPE ISSUES: none\n"
    "PRIMARY FOCUS: precision\n"
)
_PASS3_TOKENS = ["Rewrite ", "of ", "the ", "prompt."]
_PASS4_TEXT = (
    "SPECIFICITY: 9\n"
    "CONSTRAINTS: 8\n"
    "ACTIONABILITY: 9\n"
    "IMPROVEMENT: 70\n"
)


# ─── fakes ─────────────────────────────────────────────────────────────


class _PanelFakeProvider:
    """Minimal ChatProvider used by panel slots (chat-only).

    Records every chat() call; canned response is round-robin'd from
    the supplied list. ``latency_s`` lets concurrency tests detect
    overlapping calls.
    """

    name = "panel-fake"

    def __init__(self, responses: list[str], *, latency_s: float = 0.0):
        self.responses = list(responses)
        self.latency_s = latency_s
        self.calls: list[dict] = []
        self.call_times: list[tuple[float, float]] = []  # (start, end)

    async def chat(self, messages, *, model, temperature=None, max_tokens=None,
                   timeout=None):
        started = time.monotonic()
        self.calls.append({
            "messages": list(messages),
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        ended = time.monotonic()
        self.call_times.append((started, ended))
        if not self.responses:
            return ""
        # Round-robin so a panel attached to multiple passes keeps
        # producing valid Pass-1/2/4 text in turn.
        return self.responses[(len(self.calls) - 1) % len(self.responses)]

    async def chat_stream(self, *args, **kwargs) -> AsyncIterator[str]:  # pragma: no cover
        if False:
            yield ""

    async def list_models(self):
        return ["panel-fake"]

    async def context_window(self, model):
        return 8192


def _seed_pipeline(provider) -> None:
    """Enqueue minimal-pipeline responses on the OUTER provider.

    Pass 3 still streams via the outer provider in panel mode (Pass 3
    is not panelled). Pass 1/2/4 do NOT consume from the outer provider
    when a panel is wired.
    """
    provider.stream_responses.extend([_PASS3_TOKENS])


def _seed_no_panel(provider) -> None:
    """No-panel pipeline: every pass goes through chat_stream."""
    provider.stream_responses.extend([
        [_PASS1_TEXT],
        [_PASS2_TEXT],
        _PASS3_TOKENS,
        [_PASS4_TEXT],
    ])


def _build_panel(*, latency_s: float = 0.0) -> tuple[
    ReasoningPanel, _PanelFakeProvider, _PanelFakeProvider, _PanelFakeProvider,
]:
    """Build a 1-primary + 2-partner panel.

    Each slot's response list cycles through Pass-1 / Pass-2 / Pass-4
    valid text so the panel can be applied to all three panelled passes
    in one run without seeding more.
    """
    primary = _PanelFakeProvider(
        [_PASS1_TEXT, _PASS2_TEXT, _PASS4_TEXT], latency_s=latency_s,
    )
    partner_a = _PanelFakeProvider(
        ["partner-a-1", "partner-a-2", "partner-a-4"], latency_s=latency_s,
    )
    partner_b = _PanelFakeProvider(
        ["partner-b-1", "partner-b-2", "partner-b-4"], latency_s=latency_s,
    )
    panel = ReasoningPanel([
        LLMSlot("primary", primary, "panel-fake"),
        LLMSlot("partner_a", partner_a, "panel-fake"),
        LLMSlot("partner_b", partner_b, "panel-fake"),
    ])
    return panel, primary, partner_a, partner_b


# ─── tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_without_panel_unchanged(fake_provider, event_collector):
    """No panel → behavior is byte-identical to v2.0.

    Pass 1, 2, 3, 4 all go through the outer provider's chat_stream;
    extras["panel"] is absent.
    """
    _seed_no_panel(fake_provider)
    on_event, _events = event_collector

    result = await run_pipeline(
        "Build a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        reasoning_panel=None,
    )

    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    chats = [c for c in fake_provider.calls if c.kind == "chat"]
    # 4 streamed passes, no plain chat() calls (pass4 also streams here).
    assert len(streams) >= 4
    assert chats == []
    assert "panel" not in (result.extras or {})


@pytest.mark.asyncio
async def test_pipeline_with_panel_uses_panel_for_pass1_2_4(
    fake_provider, event_collector,
):
    """Panel supplied → Pass 1/2/4 route through panel; Pass 3 still streams."""
    _seed_pipeline(fake_provider)
    panel, primary, partner_a, partner_b = _build_panel()
    on_event, _events = event_collector

    await run_pipeline(
        "Build a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        reasoning_panel=panel,
        panel_mode="parallel",
        panel_aggregator="primary-wins",
    )

    # The PRIMARY slot was invoked exactly 3 times — once per panelled pass.
    assert len(primary.calls) == 3, (
        f"primary should be called for pass1/2/4 (got {len(primary.calls)})"
    )
    # Both partners were also consulted exactly 3 times each.
    assert len(partner_a.calls) == 3
    assert len(partner_b.calls) == 3

    # Pass 3 still went through the outer provider as a stream.
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    assert len(streams) == 1, (
        f"only Pass 3 should stream when panel is wired; saw {len(streams)}"
    )

    # Sanity: partners actually saw user messages (not empty).
    for call in partner_a.calls:
        assert any(m["role"] == "user" for m in call["messages"])


@pytest.mark.asyncio
async def test_pipeline_panel_telemetry_lands_in_extras(
    fake_provider, event_collector,
):
    """``result.extras['panel']`` carries per-pass telemetry in reviewer shape."""
    _seed_pipeline(fake_provider)
    panel, _primary, _partner_a, _partner_b = _build_panel()
    on_event, _events = event_collector

    result = await run_pipeline(
        "Build a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        reasoning_panel=panel,
        panel_mode="parallel",
        panel_aggregator="primary-wins",
    )

    panel_tel = (result.extras or {}).get("panel")
    assert panel_tel is not None, "panel telemetry missing from extras"
    # Three panelled passes — pass1, pass2, pass4. NOT pass3.
    assert set(panel_tel.keys()) == {"pass1", "pass2", "pass4"}

    for pass_name in ("pass1", "pass2", "pass4"):
        entry = panel_tel[pass_name]
        # Reviewer.py shape: {primary: <content>, partners: [{name, content, ms, error}]}
        assert "primary" in entry
        assert isinstance(entry["primary"], str)
        assert "partners" in entry
        assert isinstance(entry["partners"], list)
        assert len(entry["partners"]) == 2  # partner_a + partner_b
        for p in entry["partners"]:
            assert {"name", "content", "ms", "error"} <= set(p.keys())


@pytest.mark.asyncio
async def test_pipeline_panel_does_not_violate_serial_invariant(
    fake_provider, event_collector,
):
    """Even with a panel, Pass 2 must start AFTER Pass 1 ends.

    Inside a single panel.consult, slot calls run concurrently — that's
    fine. The sacred invariant is BETWEEN passes: Pass 2 cannot begin
    until Pass 1 has fully finished.
    """
    _seed_pipeline(fake_provider)
    panel, primary, _partner_a, _partner_b = _build_panel(latency_s=0.15)
    on_event, _events = event_collector

    started = time.monotonic()
    await run_pipeline(
        "Build a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        reasoning_panel=panel,
        panel_mode="parallel",
        panel_aggregator="primary-wins",
    )
    elapsed = time.monotonic() - started

    # Pass 1, 2, 4 each take ~0.15s on the primary's clock (parallel mode
    # means they overlap WITHIN a pass). With strict serial passes total
    # ≥ 3 × 0.15 = 0.45s. With parallel passes, ~0.15s.
    assert elapsed >= 0.40, (
        f"passes appear to overlap (elapsed={elapsed:.2f}s, expected ≥ 0.40s "
        "for 3 serial panelled passes at 0.15s each)"
    )

    # Direct call-time check on the primary: pass2's window must START
    # AFTER pass1's window ENDS.
    assert len(primary.call_times) >= 2
    p1_started, p1_ended = primary.call_times[0]
    p2_started, _p2_ended = primary.call_times[1]
    assert p2_started >= p1_ended, (
        f"Pass 2 (t={p2_started:.3f}) started before Pass 1 ended "
        f"(t={p1_ended:.3f}) — serial invariant violated even with panel."
    )
