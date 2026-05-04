import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from round_robin import config as rr_config
from round_robin.lm_client import LMLinkError
from round_robin.orchestrator import (
    AgentConfig,
    CharlieConfig,
    Orchestrator,
    RunConfig,
    STATUS_DONE,
)


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Redirect state file into tmp so tests don't touch real data dir."""
    monkeypatch.setattr(rr_config, "STATE_FILE", tmp_path / "state.json")
    # orchestrator imports STATE_FILE at module load time
    import round_robin.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "STATE_FILE", tmp_path / "state.json")
    yield


class FakeClient:
    def __init__(self, scripts: dict[str, list[str]] | None = None,
                 fail_models: set[str] | None = None) -> None:
        self.scripts = scripts or {}
        self.fail_models = fail_models or set()
        self.calls: list[str] = []

    async def chat_stream(self, messages, model, temperature=0.7) -> AsyncIterator[str]:
        self.calls.append(model)
        if model in self.fail_models:
            raise LMLinkError(f"simulated failure for {model}")
        for tok in self.scripts.get(model, ["ok"]):
            yield tok

    async def chat(self, messages, model, **_) -> str:
        if model in self.fail_models:
            raise LMLinkError(f"simulated failure for {model}")
        return "".join(self.scripts.get(model, ["ok"]))

    async def model_info(self, model: str):
        return None  # fallback budget path

    async def aclose(self) -> None:
        pass


def collect_events():
    events: list[tuple[str, dict[str, Any]]] = []
    async def emit(event: str, **fields):
        events.append((event, fields))
    return events, emit


async def test_full_run_two_agents():
    client = FakeClient(scripts={"m1": ["a", "b"], "m2": ["c", "d"]})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=2,
    )
    await orch.start(cfg)
    await orch._task
    types = [e[0] for e in events]
    assert types.count("turn_started") == 4
    assert types.count("turn_done") == 4
    assert any(e[0] == "run_done" and e[1]["status"] == STATUS_DONE for e in events)
    assert orch.status == STATUS_DONE


async def test_pause_after_each_turn():
    client = FakeClient(scripts={"m1": ["x"], "m2": ["y"]})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
        pause_after_each_turn=True,
    )
    await orch.start(cfg)
    # Wait until first pause appears
    for _ in range(50):
        if any(e[0] == "run_paused" for e in events):
            break
        await asyncio.sleep(0.01)
    assert any(e[0] == "run_paused" for e in events)
    await orch.resume(injection="keep going")
    # Wait for second pause or done
    for _ in range(100):
        if orch.status in (STATUS_DONE, "stopped"):
            break
        if events and events[-1][0] == "run_paused":
            await orch.resume()
        await asyncio.sleep(0.01)
    if orch._task and not orch._task.done():
        await orch._task
    assert any(e[1].get("agent") == "user_nudge"
               for e in [{"agent": x.get("agent")} for _, x in []]) or \
           any(t.get("agent") == "user_nudge" for t in orch.transcript)


async def test_error_then_skip():
    client = FakeClient(scripts={"m1": ["x"]}, fail_models={"m2"})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
    )
    await orch.start(cfg)
    for _ in range(100):
        if any(e[0] == "agent_error" for e in events):
            break
        await asyncio.sleep(0.01)
    await orch.submit_choice("skip")
    if orch._task and not orch._task.done():
        await orch._task
    assert any(t.get("skipped") for t in orch.transcript)


async def test_error_then_retry_succeeds():
    """First call to m2 fails, second succeeds (we mutate fail_models mid-run)."""
    client = FakeClient(scripts={"m1": ["x"], "m2": ["recovered"]}, fail_models={"m2"})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
    )
    await orch.start(cfg)
    for _ in range(100):
        if any(e[0] == "agent_error" for e in events):
            break
        await asyncio.sleep(0.01)
    client.fail_models.discard("m2")
    await orch.submit_choice("retry")
    if orch._task and not orch._task.done():
        await orch._task
    assert orch.status == STATUS_DONE
    bravo_lines = [t for t in orch.transcript if t.get("agent") == "Bravo"]
    assert bravo_lines and "recovered" in bravo_lines[-1]["content"]


async def test_stop_mid_run():
    async def slow_stream():
        for tok in ["1", "2", "3"]:
            await asyncio.sleep(0.05)
            yield tok

    class SlowClient(FakeClient):
        async def chat_stream(self, messages, model, temperature=0.7):
            self.calls.append(model)
            async for t in slow_stream():
                yield t

    client = SlowClient(scripts={"m1": [], "m2": []})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=10,
    )
    await orch.start(cfg)
    await asyncio.sleep(0.1)
    await orch.stop()
    assert orch.status == "stopped"


async def test_two_agents_minimum_required():
    client = FakeClient()
    _, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(theme="t", agents=[AgentConfig("Solo", "m1")])
    with pytest.raises(ValueError):
        await orch.start(cfg)


# ── Dialogue intelligence ────────────────────────────────────────────────────


async def test_nudge_fires_on_closure_phrase_with_rounds_remaining():
    """Closure phrase mid-run → orchestrator injects a continuation nudge."""
    client = FakeClient(scripts={
        "m1": ["First take", " on the design."],
        "m2": ["In summary, ", "the design is ", "solid. Let me know if you need anything else."],
    })
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=3,
    )
    await orch.start(cfg)
    await orch._task
    nudges = [e for e in events if e[0] == "dialogue_nudge"]
    assert nudges, "expected at least one dialogue_nudge"
    # First nudge should be a closure nudge fired after Bravo
    first = nudges[0][1]
    assert first["reason"] == "closure"
    assert first["after_agent"] == "Bravo"
    # Nudge ended up in transcript as user_nudge
    nudge_entries = [t for t in orch.transcript if t.get("agent") == "user_nudge"]
    assert nudge_entries
    assert nudge_entries[0].get("intel_reason") == "closure"


async def test_nudge_does_not_fire_on_last_turn():
    """Closure phrase at the very last turn → no nudge (nothing left to respond)."""
    client = FakeClient(scripts={
        "m1": ["fine"],
        "m2": ["In summary, that wraps it up."],
    })
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
    )
    await orch.start(cfg)
    await orch._task
    assert [e for e in events if e[0] == "dialogue_nudge"] == []


async def test_anti_yes_man_contrarian_after_streak():
    """Two consecutive agreements → contrarian nudge fires + streak resets."""
    client = FakeClient(scripts={
        "m1": ["I agree, great point."],          # Alpha turn 0 — agreement
        "m2": ["Spot on, makes sense."],          # Bravo turn 0 — agreement #2 → nudge
    })
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=3,
        intel_anti_rambling=False,   # isolate the agreement-streak path
        intel_agreement_threshold=2,
    )
    await orch.start(cfg)
    await orch._task
    nudges = [e for e in events if e[0] == "dialogue_nudge"]
    assert nudges
    assert nudges[0][1]["reason"] == "agreement_streak"
    # Nudge fires once per streak cycle (6 turns / threshold=2 ≈ 2-3 nudges)
    streak_nudges = [n for n in nudges if n[1]["reason"] == "agreement_streak"]
    assert 1 <= len(streak_nudges) <= 3


async def test_collab_directive_present_in_messages_when_enabled():
    from round_robin.intel import COLLAB_DIRECTIVE
    from round_robin.orchestrator import _build_messages

    agent = AgentConfig("Alpha", "m1", persona="Be terse.")
    msgs_on = _build_messages(agent, "theme", [], collab_directive=True)
    msgs_off = _build_messages(agent, "theme", [], collab_directive=False)
    assert COLLAB_DIRECTIVE in msgs_on[0]["content"]
    assert COLLAB_DIRECTIVE not in msgs_off[0]["content"]


async def test_intel_disabled_no_nudges():
    """All intel toggles off → no nudges even with closure phrases everywhere."""
    client = FakeClient(scripts={
        "m1": ["In summary, done. Let me know if anything else."],
        "m2": ["I agree, perfect, sounds good. In conclusion."],
    })
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=3,
        intel_anti_rambling=False,
        intel_anti_yes_man=False,
    )
    await orch.start(cfg)
    await orch._task
    assert [e for e in events if e[0] == "dialogue_nudge"] == []
