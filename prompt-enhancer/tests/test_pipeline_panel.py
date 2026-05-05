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
    # Partners get a 4th canned response for the v2.2 Pass 3 streaming
    # panel — partners run a non-streaming chat() call for Pass 3
    # telemetry while the primary's stream flows from the outer provider.
    partner_a = _PanelFakeProvider(
        ["partner-a-1", "partner-a-2", "partner-a-3", "partner-a-4"],
        latency_s=latency_s,
    )
    partner_b = _PanelFakeProvider(
        ["partner-b-1", "partner-b-2", "partner-b-3", "partner-b-4"],
        latency_s=latency_s,
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

    # The PRIMARY slot was invoked exactly 3 times — once per non-streaming
    # panelled pass (Pass 3 streams from the outer provider, not the panel
    # primary).
    assert len(primary.calls) == 3, (
        f"primary should be called for pass1/2/4 (got {len(primary.calls)})"
    )
    # v2.2: partners ALSO get called for Pass 3 (non-streaming, telemetry-
    # only) — so each partner sees 4 calls total.
    assert len(partner_a.calls) == 4
    assert len(partner_b.calls) == 4

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
    # v2.2: all four passes panelled (pass3 partners run non-streaming
    # chat() concurrently with primary's stream — telemetry-only).
    assert set(panel_tel.keys()) == {"pass1", "pass2", "pass3", "pass4"}

    for pass_name in ("pass1", "pass2", "pass3", "pass4"):
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


# ─── v2.2: Pass 3 streaming-panel ──────────────────────────────────────


@pytest.mark.asyncio
async def test_pass3_partners_run_concurrently_with_primary_stream(
    fake_provider, event_collector,
):
    """Pass 3: primary streams from outer provider; partners chat() in parallel.

    Concurrency proof: each partner has 0.20s latency. If they ran
    sequentially after the primary stream, total Pass-3 time would be
    ~0.40s. Concurrent → ~0.20s. Stream from outer provider is fast.
    """
    _seed_pipeline(fake_provider)
    panel, _primary, partner_a, partner_b = _build_panel(latency_s=0.20)
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

    # Each partner saw exactly one Pass 3 call (the 3rd one in the
    # round-robin response list — content "partner-a-3" / "partner-b-3").
    pass3_calls_a = [
        c for c in partner_a.calls
        if any(m["role"] == "user" and "Original prompt" in m["content"]
                for m in c["messages"])
    ]
    pass3_calls_b = [
        c for c in partner_b.calls
        if any(m["role"] == "user" and "Original prompt" in m["content"]
                for m in c["messages"])
    ]
    assert len(pass3_calls_a) == 1
    assert len(pass3_calls_b) == 1

    # Concurrency: partner_a's Pass 3 window must overlap partner_b's.
    a_window = partner_a.call_times[2]  # 0=pass1, 1=pass2, 2=pass3
    b_window = partner_b.call_times[2]
    overlap = min(a_window[1], b_window[1]) - max(a_window[0], b_window[0])
    assert overlap > 0, (
        f"partner Pass 3 windows did not overlap "
        f"(a={a_window}, b={b_window}) — partners must run concurrently"
    )


@pytest.mark.asyncio
async def test_pass3_panel_telemetry_records_streamed_primary(
    fake_provider, event_collector,
):
    """`extras['panel']['pass3']['primary']` is the streamed text from
    the outer provider — NOT a partner provider's content. Partners are
    advisory; primary's stream is canonical."""
    _seed_pipeline(fake_provider)
    panel, _primary, _a, _b = _build_panel()
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

    pass3_tel = result.extras["panel"]["pass3"]
    # _PASS3_TOKENS == ["Rewrite ", "of ", "the ", "prompt."] → joined
    assert pass3_tel["primary"] == "Rewrite of the prompt."
    # Partners' content is each provider's "partner-X-3" canned response.
    partner_contents = {p["name"]: p["content"] for p in pass3_tel["partners"]}
    assert partner_contents == {
        "partner_a": "partner-a-3",
        "partner_b": "partner-b-3",
    }


@pytest.mark.asyncio
async def test_pass3_partner_failure_does_not_break_stream(
    fake_provider, event_collector,
):
    """A crashing partner during Pass 3 must NOT affect the primary's
    stream or downstream Pass 4. Telemetry captures the error per slot."""
    _seed_pipeline(fake_provider)
    panel, _primary, partner_a, _partner_b = _build_panel()
    on_event, events = event_collector

    # Replace partner_a's chat with one that raises on the Pass 3 call
    # (the 3rd call across this run — pass1, pass2, then pass3).
    original_chat = partner_a.chat
    async def _crashing_chat(messages, **kwargs):
        partner_a.calls.append({
            "messages": list(messages), "model": kwargs.get("model"),
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
        })
        partner_a.call_times.append((time.monotonic(), time.monotonic()))
        if any(m["role"] == "user" and "Original prompt" in m["content"]
                for m in messages):
            raise RuntimeError("partner_a hates Pass 3")
        # Defer to original behavior for pass1/2/4
        return await original_chat(messages, **kwargs)
    partner_a.chat = _crashing_chat  # type: ignore[assignment]

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

    # Primary stream still produced its full content.
    pass3_tel = result.extras["panel"]["pass3"]
    assert pass3_tel["primary"] == "Rewrite of the prompt."
    # Partner_a's Pass 3 telemetry shows the error; partner_b is fine.
    by_name = {p["name"]: p for p in pass3_tel["partners"]}
    assert by_name["partner_a"]["error"] is not None
    assert "partner_a hates Pass 3" in by_name["partner_a"]["error"]
    assert by_name["partner_a"]["content"] == ""
    assert by_name["partner_b"]["error"] is None
    assert by_name["partner_b"]["content"] == "partner-b-3"

    # Pass 4 should still have run (no AGENT_ERROR step="pass3" except
    # the per-partner one captured into telemetry — pipeline-level
    # error-event accounting is unaffected).
    pass4_results = [
        kwargs for name, kwargs in events
        if name == EventType.AGENT_PASS_RESULT.value
        and kwargs.get("pass_number") == 4
    ]
    assert len(pass4_results) == 1, "Pass 4 must still complete after partner failure"


@pytest.mark.asyncio
async def test_pass3_primary_only_mode_skips_partners(
    fake_provider, event_collector,
):
    """When panel_mode='primary-only', Pass 3 partners must NOT be called.

    primary-only is the cheapest cohabitation mode — wire the panel for
    telemetry of pass1/2/4 but skip the streaming-pass overhead.
    """
    _seed_pipeline(fake_provider)
    panel, _primary, partner_a, partner_b = _build_panel()
    on_event, _events = event_collector

    result = await run_pipeline(
        "Build a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        reasoning_panel=panel,
        panel_mode="primary-only",
        panel_aggregator="primary-wins",
    )

    # In primary-only mode, partners aren't called for ANY pass.
    assert len(partner_a.calls) == 0
    assert len(partner_b.calls) == 0
    # And no pass3 panel telemetry is emitted.
    panel_tel = (result.extras or {}).get("panel", {})
    assert "pass3" not in panel_tel
