"""Tests for ``enhancer.llm.reasoning_panel``."""

from __future__ import annotations

import asyncio
import json

import pytest

from enhancer.llm.reasoning_panel import (
    DEFAULT_AGGREGATOR,
    DEFAULT_MODE,
    VALID_AGGREGATORS,
    VALID_MODES,
    LLMSlot,
    PanelResult,
    ReasoningPanel,
    SlotResponse,
)


# ─── fakes ──────────────────────────────────────────────────────────────


class _FakeProvider:
    """Minimal ChatProvider stand-in for panel tests.

    Each instance takes a `responses` list (one per chat() call). On
    exhaustion it returns the last response. Optional `raise_after_n`
    causes the (n+1)th call to raise.
    """

    name = "fake"

    def __init__(self, responses: list[str], *, raise_after_n: int | None = None):
        self.responses = responses
        self.calls: list[dict] = []
        self.raise_after_n = raise_after_n

    async def chat(self, messages, *, model, **kwargs):
        self.calls.append({"messages": list(messages), "model": model, **kwargs})
        if self.raise_after_n is not None and len(self.calls) > self.raise_after_n:
            raise RuntimeError("fake provider crash")
        if not self.responses:
            return ""
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover — unused
        if False:
            yield ""

    async def list_models(self):  # pragma: no cover — unused
        return []


def _slot(name: str, *, model: str = "fake-7b", role: str = "",
          weight: float = 1.0, responses: list[str] | None = None,
          raise_after_n: int | None = None) -> LLMSlot:
    """Convenience: build a slot with a fresh fake provider."""
    return LLMSlot(
        name=name,
        provider=_FakeProvider(responses or [f"{name}-response"],
                                raise_after_n=raise_after_n),
        model=model,
        role=role,
        weight=weight,
    )


# ─── construction + slot management ─────────────────────────────────────


def test_panel_requires_at_least_one_slot():
    with pytest.raises(ValueError, match="at least one slot"):
        ReasoningPanel([])


def test_single_slot_panel_is_just_a_primary():
    p = ReasoningPanel([_slot("primary")])
    assert len(p) == 1
    assert p.primary.name == "primary"
    assert p.partners == ()


def test_panel_is_unbounded():
    """No cap on slot count — 'infinite' is just list semantics."""
    panel = ReasoningPanel([_slot("primary")])
    for i in range(50):
        panel.add_slot(_slot(f"partner_{i}"))
    assert len(panel) == 51
    assert len(panel.partners) == 50


def test_remove_slot_returns_true_when_found():
    panel = ReasoningPanel([_slot("primary"), _slot("critic")])
    assert panel.remove_slot("critic") is True
    assert len(panel) == 1


def test_remove_slot_returns_false_when_missing():
    panel = ReasoningPanel([_slot("primary"), _slot("critic")])
    assert panel.remove_slot("absent") is False
    assert len(panel) == 2


def test_remove_primary_is_refused():
    panel = ReasoningPanel([_slot("primary"), _slot("critic")])
    with pytest.raises(ValueError, match="Cannot remove the primary"):
        panel.remove_slot("primary")


def test_slot_role_decorates_system_message():
    s = LLMSlot("critic", _FakeProvider(["x"]), "fake-7b",
                role="strict reviewer")
    assert "critic" in s.system_decoration()
    assert "strict reviewer" in s.system_decoration()


def test_slot_with_no_role_has_empty_decoration():
    s = LLMSlot("primary", _FakeProvider(["x"]), "fake-7b", role="")
    assert s.system_decoration() == ""


# ─── modes ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_primary_only_mode_skips_partners():
    primary = _slot("primary", responses=["primary-said"])
    partner = _slot("critic", responses=["critic-said"])
    panel = ReasoningPanel([primary, partner])

    result = await panel.consult(
        [{"role": "user", "content": "x"}],
        mode="primary-only",
    )

    assert result.primary.content == "primary-said"
    assert result.partners == ()
    assert result.aggregated == "primary-said"
    # Partner provider never called.
    assert len(partner.provider.calls) == 0


@pytest.mark.asyncio
async def test_parallel_mode_calls_all_slots_concurrently():
    primary = _slot("primary", responses=["p"])
    s1 = _slot("partner1", responses=["a"])
    s2 = _slot("partner2", responses=["b"])
    panel = ReasoningPanel([primary, s1, s2])

    result = await panel.consult(
        [{"role": "user", "content": "x"}],
        mode="parallel",
    )

    assert result.primary.content == "p"
    assert [pp.content for pp in result.partners] == ["a", "b"]
    # All providers got exactly one call.
    assert len(primary.provider.calls) == 1
    assert len(s1.provider.calls) == 1
    assert len(s2.provider.calls) == 1


@pytest.mark.asyncio
async def test_sequential_mode_threads_prior_outputs():
    primary = _slot("primary", responses=["P"])
    s1 = _slot("partner1", responses=["a"])
    s2 = _slot("partner2", responses=["b"])
    panel = ReasoningPanel([primary, s1, s2])

    await panel.consult(
        [{"role": "user", "content": "x"}],
        mode="sequential",
    )

    # Partner 2 must have seen primary's AND partner 1's content in
    # its message chain (added as assistant turns).
    p2_messages = s2.provider.calls[0]["messages"]
    assistant_contents = [m["content"] for m in p2_messages
                            if m["role"] == "assistant"]
    assert "P" in assistant_contents
    assert "a" in assistant_contents


# ─── aggregators ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_primary_wins_aggregator_returns_primary():
    primary = _slot("primary", responses=["short"])
    s1 = _slot("verbose", responses=["x" * 1000])
    panel = ReasoningPanel([primary, s1])

    result = await panel.consult(
        [{"role": "user", "content": "q"}],
        mode="parallel",
        aggregator="primary-wins",
    )
    assert result.aggregated == "short"


@pytest.mark.asyncio
async def test_longest_aggregator_picks_longest_response():
    primary = _slot("primary", responses=["short"])
    s1 = _slot("verbose", responses=["x" * 100])
    panel = ReasoningPanel([primary, s1])

    result = await panel.consult(
        [{"role": "user", "content": "q"}],
        mode="parallel",
        aggregator="longest",
    )
    assert result.aggregated == "x" * 100


@pytest.mark.asyncio
async def test_longest_aggregator_falls_back_to_primary_on_all_errors():
    """When every slot errored, longest aggregator still returns
    primary.content (which may be empty); never raises."""

    class _AlwaysFail:
        name = "fail"

        async def chat(self, *a, **k):
            raise RuntimeError("nope")

        async def chat_stream(self, *a, **k):  # pragma: no cover
            if False:
                yield ""

        async def list_models(self):  # pragma: no cover
            return []

    bad_primary = LLMSlot("primary", _AlwaysFail(), "x")
    bad_partner = LLMSlot("partner", _AlwaysFail(), "x")
    panel = ReasoningPanel([bad_primary, bad_partner])

    result = await panel.consult(
        [{"role": "user", "content": "q"}],
        mode="parallel",
        aggregator="longest",
    )
    # All errored; primary-wins fallback returns primary's content (empty).
    assert result.aggregated == ""
    assert result.primary.error is not None
    assert all(p.error is not None for p in result.partners)


@pytest.mark.asyncio
async def test_consensus_vote_picks_majority_value():
    """3 slots vote on a {decision: bool} object; majority wins."""
    primary = _slot("primary", responses=[json.dumps({"decision": True})])
    a = _slot("a", responses=[json.dumps({"decision": True})])
    b = _slot("b", responses=[json.dumps({"decision": False})])
    panel = ReasoningPanel([primary, a, b])

    result = await panel.consult(
        [{"role": "user", "content": "vote"}],
        mode="parallel",
        aggregator="consensus-vote",
    )
    aggregated = json.loads(result.aggregated)
    assert aggregated == {"decision": True}


@pytest.mark.asyncio
async def test_consensus_vote_respects_slot_weights():
    """Two voters say A, one heavy-weight voter says B; B wins."""
    primary = _slot("primary", responses=[json.dumps({"choice": "A"})], weight=1.0)
    a = _slot("a", responses=[json.dumps({"choice": "A"})], weight=1.0)
    b = _slot("b", responses=[json.dumps({"choice": "B"})], weight=10.0)
    panel = ReasoningPanel([primary, a, b])

    result = await panel.consult(
        [{"role": "user", "content": "vote"}],
        mode="parallel",
        aggregator="consensus-vote",
    )
    aggregated = json.loads(result.aggregated)
    assert aggregated == {"choice": "B"}


@pytest.mark.asyncio
async def test_consensus_vote_falls_back_when_too_few_parse():
    """If <2 slots produce parseable JSON, falls back to primary's content."""
    primary = _slot("primary", responses=["plain text, not json"])
    a = _slot("a", responses=["also not json"])
    panel = ReasoningPanel([primary, a])

    result = await panel.consult(
        [{"role": "user", "content": "vote"}],
        mode="parallel",
        aggregator="consensus-vote",
    )
    assert result.aggregated == "plain text, not json"


# ─── error capture ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partner_failure_does_not_kill_panel():
    """If a partner provider crashes, we still get a SlotResponse for it
    with error populated — and the primary's response is unaffected."""
    primary = _slot("primary", responses=["primary-ok"])
    bad_partner = _slot("crashy", responses=["x"], raise_after_n=0)  # crash on call 1
    panel = ReasoningPanel([primary, bad_partner])

    result = await panel.consult(
        [{"role": "user", "content": "q"}],
        mode="parallel",
    )

    assert result.primary.content == "primary-ok"
    assert result.primary.error is None
    assert len(result.partners) == 1
    assert result.partners[0].error is not None
    assert "crash" in result.partners[0].error.lower()


# ─── validation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_mode_raises():
    panel = ReasoningPanel([_slot("primary")])
    with pytest.raises(ValueError, match="Unknown mode"):
        await panel.consult([{"role": "user", "content": "q"}], mode="zzz")


@pytest.mark.asyncio
async def test_invalid_aggregator_raises():
    panel = ReasoningPanel([_slot("primary")])
    with pytest.raises(ValueError, match="Unknown aggregator"):
        await panel.consult(
            [{"role": "user", "content": "q"}],
            aggregator="zzz",
        )


def test_default_mode_and_aggregator_are_in_valid_sets():
    """Sanity guard against typos in the constants."""
    assert DEFAULT_MODE in VALID_MODES
    assert DEFAULT_AGGREGATOR in VALID_AGGREGATORS


# ─── result serialization ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_result_to_dict_round_trips():
    primary = _slot("primary", responses=["p"])
    p1 = _slot("p1", responses=["a"])
    panel = ReasoningPanel([primary, p1])

    result = await panel.consult(
        [{"role": "user", "content": "q"}],
        mode="parallel",
    )

    d = result.to_dict()
    assert d["primary"]["slot_name"] == "primary"
    assert len(d["partners"]) == 1
    assert d["partners"][0]["slot_name"] == "p1"
    assert d["mode"] == "parallel"
    assert d["aggregator"] == DEFAULT_AGGREGATOR
    assert d["total_duration_ms"] >= 0


# ─── role decoration is forwarded into the chat call ────────────────────


@pytest.mark.asyncio
async def test_slot_role_is_prepended_as_system_message():
    s = _slot("critic", role="strict code reviewer", responses=["ok"])
    panel = ReasoningPanel([_slot("primary", responses=["p"]), s])

    await panel.consult([{"role": "user", "content": "q"}], mode="parallel")

    # critic's call should have a system message containing its role.
    msgs = s.provider.calls[0]["messages"]
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert any("critic" in m["content"] and "strict" in m["content"]
                for m in system_msgs)
