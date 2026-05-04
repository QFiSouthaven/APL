"""Integration tests for the round-robin ReasoningPanel wiring.

These tests verify:

* The path-injection re-export module imports cleanly.
* ``Orchestrator.start(reasoning_panel=None)`` is byte-equivalent to the
  pre-panel 2-LLM dialogue (regression guard).
* ``Orchestrator.start(reasoning_panel=panel)`` extends the dialogue to
  one turn per slot in panel order.
* ``review_with_dialogue(reasoning_panel=None)`` is byte-equivalent to
  the v0.4 3-call review (regression guard).
* ``review_with_dialogue(reasoning_panel=panel)`` calls ``len(panel) + 1``
  LLMs (per-slot verdicts in parallel + consensus reconciler) and the
  ``agents`` block carries one entry per slot plus ``consensus``.
* Slot-error tolerance: if one slot crashes, the review still produces a
  unified verdict (best-effort).
* Each slot's role/system-prompt is forwarded into the chat call.

We use a local FakeLMClient and a local _FakePanelProvider — we never
import test fakes from the prompt-enhancer repo.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from round_robin import config as rr_config
from round_robin.code_review import review_with_dialogue
from round_robin.lm_client import LMLinkError
from round_robin.orchestrator import (
    AgentConfig,
    Orchestrator,
    RunConfig,
    STATUS_DONE,
)


# ── Path-injection sanity check ─────────────────────────────────────────


def test_reasoning_panel_module_imports_cleanly():
    """The path-injection re-export should yield real classes, not Nones."""
    from round_robin.reasoning_panel import (
        DEFAULT_AGGREGATOR,
        DEFAULT_MODE,
        VALID_AGGREGATORS,
        VALID_MODES,
        LLMSlot,
        PanelResult,
        ReasoningPanel,
        SlotResponse,
    )

    assert LLMSlot is not None
    assert PanelResult is not None
    assert ReasoningPanel is not None
    assert SlotResponse is not None
    assert DEFAULT_MODE in VALID_MODES
    assert DEFAULT_AGGREGATOR in VALID_AGGREGATORS

    # The re-exported types should be the real ones from prompt-enhancer.
    assert LLMSlot.__module__ == "enhancer.llm.reasoning_panel"
    assert ReasoningPanel.__module__ == "enhancer.llm.reasoning_panel"


# ── Local fakes ─────────────────────────────────────────────────────────


class FakeLMClient:
    """Round-robin LMLinkClient stand-in. Records every chat() call.

    Pass ``responses`` as a list of strings; calls are answered in order.
    Exception instances are raised. Streaming returns a single chunk.
    """

    def __init__(self, responses: list | None = None,
                 *, scripts: dict[str, list[str]] | None = None) -> None:
        self.responses = list(responses) if responses else []
        self.scripts = scripts or {}
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

    async def chat_stream(self, messages, model, temperature=0.7) -> AsyncIterator[str]:
        self.calls.append({
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "stream": True,
        })
        for tok in self.scripts.get(model, ["ok"]):
            yield tok

    async def model_info(self, model: str):
        return None

    async def aclose(self) -> None:
        pass


class FakePanelProvider:
    """Minimal ChatProvider stand-in for panel slot tests.

    Each call records its arguments and returns the next canned response.
    On exhaustion it returns the last response. ``raise_after_n`` causes
    the (n+1)th call to raise.
    """

    name = "fake-panel"

    def __init__(
        self,
        responses: list[str],
        *,
        raise_after_n: int | None = None,
        raise_immediately: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.raise_after_n = raise_after_n
        self.raise_immediately = raise_immediately

    async def chat(self, messages, *, model, **kwargs) -> str:
        self.calls.append({"messages": list(messages), "model": model, **kwargs})
        if self.raise_immediately is not None:
            raise self.raise_immediately
        if self.raise_after_n is not None and len(self.calls) > self.raise_after_n:
            raise RuntimeError("fake panel provider crash")
        if not self.responses:
            return ""
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover — unused
        if False:
            yield ""

    async def list_models(self):  # pragma: no cover — unused
        return []


def _make_panel(slots_spec: list[dict]):
    """Build a ReasoningPanel from a list of slot specs.

    Each spec is a dict like::

        {"name": "primary", "model": "fake-7b", "role": "",
         "responses": ["..."], "raise_after_n": None}
    """
    from round_robin.reasoning_panel import LLMSlot, ReasoningPanel

    slots = []
    for spec in slots_spec:
        provider = FakePanelProvider(
            spec.get("responses") or [f"{spec['name']}-said"],
            raise_after_n=spec.get("raise_after_n"),
            raise_immediately=spec.get("raise_immediately"),
        )
        slots.append(LLMSlot(
            name=spec["name"],
            provider=provider,
            model=spec.get("model", "fake-7b"),
            role=spec.get("role", ""),
            weight=spec.get("weight", 1.0),
        ))
    return ReasoningPanel(slots), [s.provider for s in slots]


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


# ── Orchestrator: state file isolation ─────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Redirect state file into tmp so tests don't touch real data dir."""
    monkeypatch.setattr(rr_config, "STATE_FILE", tmp_path / "state.json")
    import round_robin.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "STATE_FILE", tmp_path / "state.json")
    yield


def _collect_events():
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, **fields):
        events.append((event, fields))

    return events, emit


# ── Orchestrator: regression — None panel preserves 2-LLM behavior ─────


async def test_orchestrate_with_no_panel_matches_2llm_baseline():
    """``reasoning_panel=None`` (default) must produce the byte-identical
    dialogue the 2-LLM tests exercise — same number of turns, same agent
    names, same client.chat_stream call count.
    """
    client = FakeLMClient(scripts={"m1": ["a", "b"], "m2": ["c", "d"]})
    events, emit = _collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=2,
    )
    # Default call: no reasoning_panel.
    await orch.start(cfg)
    await orch._task

    types = [e[0] for e in events]
    assert types.count("turn_started") == 4
    assert types.count("turn_done") == 4
    assert orch.status == STATUS_DONE

    # All LLM calls went through the LMLinkClient (chat_stream), NOT
    # through panel slot providers (they don't exist on this run).
    assert all(c.get("stream") is True for c in client.calls)
    assert len(client.calls) == 4


# ── Orchestrator: panel produces N-turn dialogue ───────────────────────


async def test_orchestrate_with_4_slot_panel_produces_4_turn_dialogue():
    """A 4-slot panel + loop_limit=1 should produce exactly 4 turns —
    one per slot — and route each turn through that slot's provider.
    """
    panel, providers = _make_panel([
        {"name": "Slot0", "model": "m0", "responses": ["s0-said"]},
        {"name": "Slot1", "model": "m1", "responses": ["s1-said"]},
        {"name": "Slot2", "model": "m2", "responses": ["s2-said"]},
        {"name": "Slot3", "model": "m3", "responses": ["s3-said"]},
    ])
    client = FakeLMClient()  # Should NOT be called when panel is in use.
    events, emit = _collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="testing the panel",
        # config.agents will be overridden by the panel.
        agents=[AgentConfig("placeholder1", "x"), AgentConfig("placeholder2", "y")],
        loop_limit=1,
    )
    await orch.start(cfg, reasoning_panel=panel)
    await orch._task

    types = [e[0] for e in events]
    # Expect exactly 4 turn_started + 4 turn_done events (one per slot).
    assert types.count("turn_started") == 4, f"events: {types}"
    assert types.count("turn_done") == 4, f"events: {types}"
    assert orch.status == STATUS_DONE

    # Transcript has 4 agent turns (slot order preserved).
    agent_names = [
        e["agent"] for e in orch.transcript
        if e.get("agent") not in ("orchestrator", "user_nudge")
    ]
    assert agent_names == ["Slot0", "Slot1", "Slot2", "Slot3"]

    # Each slot's provider was called exactly once; client.chat_stream was
    # NOT used at all.
    for p in providers:
        assert len(p.calls) == 1
    assert all(not c.get("stream") for c in client.calls)


async def test_orchestrate_panel_forwards_role_into_system_message():
    """Slot role should be threaded into the AgentConfig.persona, which
    becomes the system message — verify via captured chat call args."""
    panel, providers = _make_panel([
        {"name": "Primary", "model": "m0", "role": "pragmatic engineer",
         "responses": ["p"]},
        {"name": "Critic", "model": "m1", "role": "rigorous code critic",
         "responses": ["c"]},
    ])
    client = FakeLMClient()
    _, emit = _collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("placeholder", "x"), AgentConfig("placeholder2", "y")],
        loop_limit=1,
    )
    await orch.start(cfg, reasoning_panel=panel)
    await orch._task

    # Critic's system message should mention the role.
    critic_call = providers[1].calls[0]
    sys_msgs = [m for m in critic_call["messages"] if m["role"] == "system"]
    assert sys_msgs, "expected a system message"
    sys_blob = "\n".join(m["content"] for m in sys_msgs)
    assert "rigorous code critic" in sys_blob


# ── code_review: regression — None panel preserves v0.4 behavior ───────


async def test_review_with_no_panel_matches_v0_4_baseline():
    """The 3-call dialogue (A → B → Consensus) must remain unchanged when
    ``reasoning_panel=None`` (default).
    """
    client = FakeLMClient([
        _verdict_json(True, summary="A: ok"),
        _verdict_json(True, summary="B: ok"),
        _consensus_json(True, consensus="both engineers approved"),
    ])
    out = await review_with_dialogue(
        "core", "do core things", {"a.py": "x = 1"}, lm_client=client,
    )
    assert out["approved"] is True
    assert out["request_regenerate"] is False
    assert out["issues"] == []
    # v0.4 contract: agent_a_verdict + agent_b_verdict + consensus.
    assert set(out["agents"].keys()) == {
        "agent_a_verdict", "agent_b_verdict", "consensus",
    }
    # Exactly 3 LM calls — no extra slot calls.
    assert len(client.calls) == 3


# ── code_review: panel grows agents block + makes N+1 calls ────────────


async def test_review_with_5_slot_panel_makes_6_calls_total():
    """5 slots emit verdicts in parallel + 1 consensus call = 6 total
    LM calls when an lm_client is supplied as the consensus reconciler.
    """
    panel, providers = _make_panel([
        {"name": f"Slot{i}", "model": f"m{i}",
         "responses": [_verdict_json(True, summary=f"S{i}")]}
        for i in range(5)
    ])
    # Consensus call goes through lm_client.
    client = FakeLMClient([_consensus_json(True, consensus="all approved")])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."}, lm_client=client, reasoning_panel=panel,
    )
    # 5 slot calls + 1 consensus call.
    total_slot_calls = sum(len(p.calls) for p in providers)
    assert total_slot_calls == 5
    assert len(client.calls) == 1
    assert out["approved"] is True


async def test_review_panel_agents_block_has_one_entry_per_slot_plus_consensus():
    """5 slots → ``agents`` keys: agent_0..agent_4 + consensus."""
    panel, _ = _make_panel([
        {"name": f"Slot{i}", "model": f"m{i}",
         "responses": [_verdict_json(True, summary=f"S{i}")]}
        for i in range(5)
    ])
    client = FakeLMClient([_consensus_json(True, consensus="ok")])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."}, lm_client=client, reasoning_panel=panel,
    )
    expected_keys = {f"agent_{i}_verdict" for i in range(5)} | {"consensus"}
    assert set(out["agents"].keys()) == expected_keys
    assert out["agents"]["consensus"] == "ok"


async def test_review_panel_aggregation_any_rejection_rejects():
    """5 slots; one rejects → unified verdict rejects (any-rejection rule)."""
    panel, _ = _make_panel([
        {"name": "S0", "model": "m", "responses": [_verdict_json(True)]},
        {"name": "S1", "model": "m", "responses": [_verdict_json(True)]},
        {"name": "S2", "model": "m",
         "responses": [_verdict_json(False, issues=["bug here"])]},
        {"name": "S3", "model": "m", "responses": [_verdict_json(True)]},
        {"name": "S4", "model": "m", "responses": [_verdict_json(True)]},
    ])
    client = FakeLMClient([_consensus_json(True, consensus="LLM said yes but rules say no")])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."}, lm_client=client, reasoning_panel=panel,
    )
    # Even though the LLM consensus said approved=true, the deterministic
    # rule wins: any-rejection-rejects.
    assert out["approved"] is False
    assert "bug here" in out["issues"]


async def test_review_panel_tolerates_one_crashing_slot():
    """If a slot's provider raises, the panel review still produces a
    verdict (best-effort, like the existing fallback)."""
    panel, providers = _make_panel([
        {"name": "S0", "model": "m", "responses": [_verdict_json(True)]},
        {"name": "BadSlot", "model": "m",
         "raise_immediately": RuntimeError("simulated crash")},
        {"name": "S2", "model": "m", "responses": [_verdict_json(True)]},
    ])
    client = FakeLMClient([_consensus_json(True, consensus="2-of-3 approved")])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."}, lm_client=client, reasoning_panel=panel,
    )
    # The crashing slot returns the empty-string fallback which normalizes
    # to the default-approved verdict (same as a parse-failure fallback in
    # the v0.4 path). Pipeline still returns a clean dict.
    assert "approved" in out
    assert "issues" in out
    assert "agents" in out
    assert len(out["agents"]) == 4  # 3 slots + consensus
    # The good slots' providers were each called exactly once.
    assert len(providers[0].calls) == 1
    assert len(providers[2].calls) == 1


async def test_review_panel_slot_role_and_system_prompt_forwarded():
    """Each slot's call should carry its role decoration + the canonical
    review system prompt (AGENT_A for slot 0, AGENT_B for the rest)."""
    from round_robin.code_review import AGENT_A_SYSTEM, AGENT_B_SYSTEM

    panel, providers = _make_panel([
        {"name": "Primary", "model": "m0", "role": "pragmatic engineer",
         "responses": [_verdict_json(True)]},
        {"name": "Critic", "model": "m1", "role": "rigorous code critic",
         "responses": [_verdict_json(True)]},
    ])
    client = FakeLMClient([_consensus_json(True, consensus="ok")])
    await review_with_dialogue(
        "x", "p", {"f.py": "."}, lm_client=client, reasoning_panel=panel,
    )

    # Slot 0: AGENT_A_SYSTEM in the system message + role decoration.
    s0_msgs = providers[0].calls[0]["messages"]
    s0_sys = next(m["content"] for m in s0_msgs if m["role"] == "system")
    assert AGENT_A_SYSTEM in s0_sys
    assert "pragmatic engineer" in s0_sys

    # Slot 1: AGENT_B_SYSTEM in the system message + role decoration.
    s1_msgs = providers[1].calls[0]["messages"]
    s1_sys = next(m["content"] for m in s1_msgs if m["role"] == "system")
    assert AGENT_B_SYSTEM in s1_sys
    assert "rigorous code critic" in s1_sys
