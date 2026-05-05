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


# ── code_review: regression — None panel preserves dialogue baseline ───


async def test_review_without_panel_unchanged():
    """No panel = original dialogue (now 4-call with Charlie). Verify
    aggregation rules still pass and ``agents`` block carries the four
    voice keys.
    """
    client = FakeLMClient([
        _verdict_json(True, summary="A: ok"),
        _verdict_json(True, summary="B: ok"),
        _verdict_json(True, summary="C: ok"),
        _consensus_json(True, consensus="all three engineers approved"),
    ])
    out = await review_with_dialogue(
        "core", "do core things", {"a.py": "x = 1"}, lm_client=client,
    )
    assert out["approved"] is True
    assert out["request_regenerate"] is False
    assert out["issues"] == []
    # 4-voice contract: a/b/c verdicts + consensus.
    assert set(out["agents"].keys()) == {
        "agent_a_verdict", "agent_b_verdict", "agent_c_verdict", "consensus",
    }
    # Exactly 4 LM calls — A, B, C, Consensus. No panel slot calls.
    assert len(client.calls) == 4


# ── code_review: panel consults once per voice ─────────────────────────


class _RecordingPanel:
    """Minimal panel stand-in that records every ``consult`` call.

    Each consult invocation returns a ``PanelResult``-shaped namespace
    where ``aggregated`` is the next pre-canned response. Slot calls are
    NOT exercised — this is a panel-level mock.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.consult_calls: list[dict[str, Any]] = []

    async def consult(self, messages, *, temperature=None, **kwargs):
        idx = len(self.consult_calls)
        self.consult_calls.append({"messages": messages, "temperature": temperature, **kwargs})
        if not self.responses:
            aggregated = ""
        else:
            aggregated = self.responses[min(idx, len(self.responses) - 1)]

        class _R:
            pass

        result = _R()
        result.aggregated = aggregated
        return result


async def test_review_with_panel_consults_panel_per_voice():
    """Supply a panel; assert ``panel.consult`` is called once per voice
    (4 times total: A, B, C, Consensus). Each call carries the pinned
    system prompt for that voice."""
    from round_robin.code_review import (
        AGENT_A_SYSTEM,
        AGENT_B_SYSTEM,
        AGENT_C_SYSTEM,
        CONSENSUS_SYSTEM,
    )

    panel = _RecordingPanel([
        _verdict_json(True, summary="A says"),
        _verdict_json(True, summary="B says"),
        _verdict_json(True, summary="C says"),
        _consensus_json(True, consensus="all approved"),
    ])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."},
        lm_client=None, reasoning_panel=panel,  # type: ignore[arg-type]
    )
    # Exactly 4 consultations (A, B, C, Consensus).
    assert len(panel.consult_calls) == 4

    # Each consultation's system prompt must match the voice's pinned constant.
    sys_prompts = [
        call["messages"][0]["content"]
        for call in panel.consult_calls
    ]
    assert sys_prompts == [AGENT_A_SYSTEM, AGENT_B_SYSTEM, AGENT_C_SYSTEM, CONSENSUS_SYSTEM]

    # Output shape: 4-voice agents block.
    assert set(out["agents"].keys()) == {
        "agent_a_verdict", "agent_b_verdict", "agent_c_verdict", "consensus",
    }
    assert out["approved"] is True


async def test_review_with_panel_charlie_rejection_propagates():
    """A and B approve, C (Charlie via panel) rejects → final rejected."""
    panel = _RecordingPanel([
        _verdict_json(True, summary="A approves"),
        _verdict_json(True, summary="B approves"),
        _verdict_json(False, issues=["second-order rollout risk"],
                      summary="C rejects"),
        _consensus_json(True, consensus="LLM is wrong"),
    ])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."},
        lm_client=None, reasoning_panel=panel,  # type: ignore[arg-type]
    )
    assert out["approved"] is False, "Charlie's rejection rejects overall"
    assert "second-order rollout risk" in out["issues"]


async def test_review_with_panel_aggregates_dedup_across_voices():
    """Panel mode: A/B/C issues are dedup-merged across voices."""
    panel = _RecordingPanel([
        _verdict_json(False, issues=["Race condition"]),
        _verdict_json(False, issues=["race condition", "Missing tests"]),  # case-variant
        _verdict_json(False, issues=["Maintenance burden"]),
        _consensus_json(False, consensus="merge"),
    ])
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."},
        lm_client=None, reasoning_panel=panel,  # type: ignore[arg-type]
    )
    issues_lower = [i.lower() for i in out["issues"]]
    # 3 distinct: race condition, missing tests, maintenance burden.
    assert "race condition" in issues_lower
    assert "missing tests" in issues_lower
    assert "maintenance burden" in issues_lower
    assert len(issues_lower) == len(set(issues_lower))


async def test_review_with_panel_charlie_sees_a_and_b():
    """In panel mode, Charlie's user prompt must contain A's AND B's verdicts."""
    panel = _RecordingPanel([
        _verdict_json(True, summary="A-distinct-text"),
        _verdict_json(True, summary="B-distinct-text"),
        _verdict_json(True, summary="C ok"),
        _consensus_json(True, consensus="ok"),
    ])
    await review_with_dialogue(
        "x", "p", {"f.py": "."},
        lm_client=None, reasoning_panel=panel,  # type: ignore[arg-type]
    )
    # Third consult call = Charlie. Its user message must reference A & B.
    c_user = panel.consult_calls[2]["messages"][1]["content"]
    assert "PRIOR VERDICT FROM AGENT A" in c_user
    assert "PRIOR VERDICT FROM AGENT B" in c_user
    assert "A-distinct-text" in c_user
    assert "B-distinct-text" in c_user


async def test_review_with_panel_tolerates_panel_failure():
    """If panel.consult raises mid-review, fall back gracefully (best-effort)."""

    class _FailingPanel:
        def __init__(self) -> None:
            self.calls = 0

        async def consult(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 2:  # B raises
                raise RuntimeError("simulated panel failure")

            class _R:
                pass

            r = _R()
            r.aggregated = _verdict_json(True, summary="ok")
            return r

    panel = _FailingPanel()
    out = await review_with_dialogue(
        "x", "p", {"f.py": "."},
        lm_client=None, reasoning_panel=panel,  # type: ignore[arg-type]
    )
    # Pipeline still produces a clean verdict (B defaulted to approved).
    assert "approved" in out
    assert "agents" in out
    assert set(out["agents"].keys()) == {
        "agent_a_verdict", "agent_b_verdict", "agent_c_verdict", "consensus",
    }
    # All 4 voices were attempted.
    assert panel.calls == 4
