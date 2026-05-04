"""Charlie end-of-run summarizer tests."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from round_robin import config as rr_config
from round_robin.charlie import CharlieAgent, CharlieWorkspace, SUMMARY_FILENAME
from round_robin.charlie.agent import _build_frontmatter, _format_transcript
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
    monkeypatch.setattr(rr_config, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rr_config, "SANDBOX_DIR", tmp_path / "charlie_workspace")
    import round_robin.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "STATE_FILE", tmp_path / "state.json")
    import round_robin.charlie.workspace as ws_mod
    monkeypatch.setattr(ws_mod, "SANDBOX_DIR", tmp_path / "charlie_workspace")
    yield


# ── helpers ─────────────────────────────────────────────────────────────────


class FakeClient:
    def __init__(
        self,
        scripts: dict[str, list[str]] | None = None,
        chat_response: str = "",
        fail_models: set[str] | None = None,
        model_infos: dict[str, dict] | None = None,
    ) -> None:
        self.scripts = scripts or {}
        self.chat_response = chat_response
        self.fail_models = fail_models or set()
        self.chat_calls: list[tuple[list[dict], str]] = []
        # Map model id → /api/v0/models/{id} response. None means "not exposed"
        # which exercises the fallback budgeting path in CharlieAgent.
        self.model_infos = model_infos or {}

    async def chat_stream(self, messages, model, temperature=0.7) -> AsyncIterator[str]:
        if model in self.fail_models:
            raise LMLinkError(f"fail {model}")
        for tok in self.scripts.get(model, ["ok"]):
            yield tok

    async def chat(self, messages, model, **_) -> str:
        self.chat_calls.append((messages, model))
        if model in self.fail_models:
            raise LMLinkError(f"fail {model}")
        return self.chat_response

    async def model_info(self, model: str):
        return self.model_infos.get(model)

    async def aclose(self) -> None:
        pass


def collect_events():
    events: list[tuple[str, dict[str, Any]]] = []
    async def emit(event: str, **fields):
        events.append((event, fields))
    return events, emit


SAMPLE_BODY = """## Theme
Test theme

## Participants
- Alpha (model: m1)
- Bravo (model: m2)

## Resolved Decisions
- Use option A

## Proposed Module Breakdown
- ModuleX — does Y

## Open Questions
- (none)

## Full Transcript
**Alpha:** hello
**Bravo:** hi
"""


# ── unit: pure helpers ──────────────────────────────────────────────────────


def test_format_transcript_strips_orchestrator_and_nudges():
    transcript = [
        {"agent": "orchestrator", "content": "Theme: t"},
        {"agent": "Alpha", "content": "first"},
        {"agent": "user_nudge", "content": "keep going"},
        {"agent": "Bravo", "content": "second"},
        {"agent": "Alpha", "content": ""},  # empty content skipped
    ]
    out = _format_transcript(transcript)
    assert "[Alpha]" in out
    assert "[Bravo]" in out
    assert "first" in out and "second" in out
    assert "orchestrator" not in out
    assert "user_nudge" not in out
    assert "keep going" not in out


def test_frontmatter_contains_required_keys():
    fm = _build_frontmatter(run_id="abc123", theme="Cool theme", model="m-x")
    assert fm.startswith("---\n")
    assert "run_id: abc123" in fm
    assert "theme: Cool theme" in fm
    assert "model: m-x" in fm
    assert "schema_version: 1" in fm
    assert fm.rstrip().endswith("---")


def test_frontmatter_handles_multiline_theme():
    fm = _build_frontmatter(run_id=None, theme="line1\nline2", model="m")
    assert "theme: line1 line2" in fm
    assert "run_id: (none)" in fm


# ── unit: CharlieAgent.summarize ────────────────────────────────────────────


async def test_summarize_writes_summary_md_with_required_sections(tmp_path):
    client = FakeClient(chat_response=SAMPLE_BODY)
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()

    transcript = [
        {"agent": "orchestrator", "content": "Theme: t"},
        {"agent": "Alpha", "content": "hi"},
        {"agent": "Bravo", "content": "hello"},
    ]
    path = await agent.summarize(
        workspace=ws, transcript=transcript, theme="t",
        model="m-charlie", run_id="run-xyz", emit=emit,
    )

    assert path == SUMMARY_FILENAME
    written = (ws.root / SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert written.startswith("---\n")
    assert "run_id: run-xyz" in written
    assert "schema_version: 1" in written
    for section in (
        "## Theme",
        "## Participants",
        "## Resolved Decisions",
        "## Proposed Module Breakdown",
        "## Open Questions",
        "## Full Transcript",
    ):
        assert section in written, f"missing section: {section}"

    types = [e[0] for e in events]
    assert "charlie_started" in types
    assert "charlie_done" in types
    done = next(e[1] for e in events if e[0] == "charlie_done")
    assert done["path"] == SUMMARY_FILENAME
    assert done["run_id"] == "run-xyz"


async def test_summarize_emits_error_on_llm_failure():
    client = FakeClient(fail_models={"m-charlie"})
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()

    path = await agent.summarize(
        workspace=ws, transcript=[{"agent": "Alpha", "content": "x"}],
        theme="t", model="m-charlie", run_id="r", emit=emit,
    )

    assert path is None
    assert any(e[0] == "charlie_error" for e in events)
    assert not (ws.root / SUMMARY_FILENAME).exists()


async def test_summarize_emits_error_on_empty_response():
    client = FakeClient(chat_response="   ")
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()

    path = await agent.summarize(
        workspace=ws, transcript=[{"agent": "Alpha", "content": "x"}],
        theme="t", model="m", run_id="r", emit=emit,
    )

    assert path is None
    assert any(e[0] == "charlie_error" for e in events)


async def test_summarize_skips_when_busy():
    client = FakeClient(chat_response=SAMPLE_BODY)
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()
    agent._busy = True   # simulate concurrent run

    path = await agent.summarize(
        workspace=ws, transcript=[{"agent": "Alpha", "content": "x"}],
        theme="t", model="m", run_id="r", emit=emit,
    )
    assert path is None
    assert any(e[0] == "charlie_error" for e in events)


# ── integration: orchestrator end-of-run hook ───────────────────────────────


async def test_orchestrator_runs_summary_on_run_done():
    """Charlie enabled + at least one agent turn → summary fires on run_done."""
    client = FakeClient(
        scripts={"m1": ["aa"], "m2": ["bb"]},
        chat_response=SAMPLE_BODY,
    )
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="theme-x",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
        charlie=CharlieConfig(enabled=True, model="m-charlie"),
    )
    await orch.start(cfg)
    await orch._task

    types = [e[0] for e in events]
    assert orch.status == STATUS_DONE
    assert types.count("charlie_started") == 1
    assert types.count("charlie_done") == 1
    # Charlie's done must come before run_done
    assert types.index("charlie_done") < types.index("run_done")
    assert orch.summary_path == SUMMARY_FILENAME
    assert orch._charlie_workspace is not None
    assert (orch._charlie_workspace.root / SUMMARY_FILENAME).is_file()


async def test_orchestrator_skips_summary_when_charlie_disabled():
    client = FakeClient(scripts={"m1": ["aa"], "m2": ["bb"]})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
    )
    await orch.start(cfg)
    await orch._task
    assert all(not e[0].startswith("charlie_") for e in events)
    assert orch.summary_path is None


async def test_orchestrator_skips_summary_when_no_model_configured():
    client = FakeClient(scripts={"m1": ["aa"], "m2": ["bb"]})
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
        charlie=CharlieConfig(enabled=True, model=""),
    )
    await orch.start(cfg)
    await orch._task
    assert all(not e[0].startswith("charlie_") for e in events)


async def test_no_mid_run_charlie_trigger_on_confirmed_keyword():
    """Old behavior: 'Confirmed' in dialogue would trigger Charlie. Must NOT happen now."""
    client = FakeClient(
        scripts={
            "m1": ["Confirmed", " — let's proceed"],
            "m2": ["agreed"],
        },
        chat_response=SAMPLE_BODY,
    )
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="t",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
        charlie=CharlieConfig(enabled=True, model="m-charlie"),
    )
    await orch.start(cfg)
    await orch._task

    # Charlie fires exactly once (at end-of-run), not on "Confirmed".
    types = [e[0] for e in events]
    assert types.count("charlie_started") == 1
    assert types.count("charlie_done") == 1
    # And it fires AFTER the last turn_done.
    last_turn_done_idx = max(i for i, t in enumerate(types) if t == "turn_done")
    assert types.index("charlie_started") > last_turn_done_idx


async def test_regenerate_summary_idle_required():
    """Calling regenerate_summary while run config is None / never started should raise."""
    client = FakeClient(chat_response=SAMPLE_BODY)
    _, emit = collect_events()
    orch = Orchestrator(client, emit)
    with pytest.raises(ValueError):
        # No model resolvable — cfg is None and no model passed.
        await orch.regenerate_summary()


async def test_regenerate_summary_after_run():
    client = FakeClient(
        scripts={"m1": ["aa"], "m2": ["bb"]},
        chat_response=SAMPLE_BODY,
    )
    events, emit = collect_events()
    orch = Orchestrator(client, emit)
    cfg = RunConfig(
        theme="theme-x",
        agents=[AgentConfig("Alpha", "m1"), AgentConfig("Bravo", "m2")],
        loop_limit=1,
        charlie=CharlieConfig(enabled=True, model="m-charlie"),
    )
    await orch.start(cfg)
    await orch._task
    # Now manually re-run with a different model
    client.chat_response = SAMPLE_BODY.replace("Test theme", "Regenerated")
    path = await orch.regenerate_summary(model="m-charlie-2")
    assert path == SUMMARY_FILENAME
    written = (orch._charlie_workspace.root / SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert "Regenerated" in written
    assert "model: m-charlie-2" in written


# ── Phase 2: truncation + progress events ───────────────────────────────────


async def test_summarize_truncates_oversized_transcript():
    """A transcript past the token limit gets truncated; first/last turns kept,
    middle replaced with an orchestrator marker, summary frontmatter has truncated:true."""
    client = FakeClient(chat_response=SAMPLE_BODY)
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()

    # 20 agent turns, each ~100 whitespace tokens = ~2000 tokens total.
    # With token_limit=200 we should drop most of the middle.
    transcript = [{"agent": "orchestrator", "content": "Theme: t"}]
    for i in range(20):
        agent_name = "Alpha" if i % 2 == 0 else "Bravo"
        content = f"turn{i} " + "word " * 100
        transcript.append({"agent": agent_name, "content": content.strip()})

    path = await agent.summarize(
        workspace=ws, transcript=transcript, theme="t",
        model="m-charlie", run_id="run-trunc", emit=emit,
        token_limit=200,
    )

    assert path == SUMMARY_FILENAME
    written = (ws.root / SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert "truncated: true" in written
    assert "dropped_turns:" in written
    # First two and last six agent turns kept (default keep_head=2, keep_tail=6)
    # so dropped should be 20 - 8 = 12.
    assert "dropped_turns: 12" in written

    # The user prompt sent to chat() must contain the orchestrator truncation marker
    last_call_messages = client.chat_calls[-1][0]
    user_msg = last_call_messages[-1]["content"]
    # The marker is filtered out by _format_transcript (orchestrator entries are
    # stripped) — but kept-head and kept-tail turns must be present.
    assert "turn0" in user_msg
    assert "turn1" in user_msg
    assert "turn19" in user_msg
    assert "turn14" in user_msg   # last 6 = turns 14..19
    assert "turn5" not in user_msg  # middle gone

    # charlie_done payload reports truncation
    done = next(e[1] for e in events if e[0] == "charlie_done")
    assert done["truncated"] is True
    assert done["dropped_turns"] == 12

    # charlie_progress events fired in order: budgeting → truncated → calling_llm → writing
    progress_phases = [e[1]["phase"] for e in events if e[0] == "charlie_progress"]
    assert progress_phases == ["budgeting", "truncated", "calling_llm", "writing"]


async def test_summarize_no_truncation_when_under_limit():
    """Small transcript: no truncation, no `truncated` frontmatter line, no
    `truncated` progress event."""
    client = FakeClient(chat_response=SAMPLE_BODY)
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()

    transcript = [
        {"agent": "orchestrator", "content": "Theme: t"},
        {"agent": "Alpha", "content": "small one"},
        {"agent": "Bravo", "content": "small two"},
    ]
    path = await agent.summarize(
        workspace=ws, transcript=transcript, theme="t",
        model="m", run_id="r", emit=emit, token_limit=10000,
    )
    assert path == SUMMARY_FILENAME
    written = (ws.root / SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert "truncated: true" not in written

    progress_phases = [e[1]["phase"] for e in events if e[0] == "charlie_progress"]
    assert "truncated" not in progress_phases
    assert "budgeting" in progress_phases
    assert "calling_llm" in progress_phases
    assert "writing" in progress_phases


def test_truncate_helper_preserves_head_and_tail():
    """Direct unit test of the pure truncation helper."""
    from round_robin.charlie.agent import _truncate_transcript

    transcript = [
        {"agent": "orchestrator", "content": "Theme"},
        *[{"agent": "Alpha" if i % 2 == 0 else "Bravo",
           "content": "word " * 50} for i in range(15)],
    ]
    out, dropped = _truncate_transcript(transcript, token_limit=100,
                                         keep_head=2, keep_tail=3)
    assert dropped == 15 - 5
    agent_contents = [e["content"] for e in out
                      if e.get("agent") not in ("orchestrator", "user_nudge")]
    # 2 head + 3 tail = 5 agent entries
    assert len(agent_contents) == 5
    # Marker present once in the output as an orchestrator entry
    markers = [e for e in out if e.get("agent") == "orchestrator"
               and "truncated" in (e.get("content") or "")]
    assert len(markers) == 1


def test_truncate_helper_skips_when_under_limit():
    from round_robin.charlie.agent import _truncate_transcript

    transcript = [
        {"agent": "orchestrator", "content": "Theme"},
        {"agent": "Alpha", "content": "tiny"},
        {"agent": "Bravo", "content": "tiny"},
    ]
    out, dropped = _truncate_transcript(transcript, token_limit=100)
    assert dropped == 0
    assert out is transcript


# ── Phase 3: real LM Studio budget via /api/v0/models ───────────────────────


async def test_resolve_input_budget_uses_loaded_context_length():
    """When /api/v0/models exposes loaded_context_length, the budget is derived
    from it (minus output reserve), not the heuristic CHARLIE_INPUT_TOKEN_LIMIT."""
    from round_robin.charlie.agent import (
        CHARLIE_OUTPUT_RESERVE_TOKENS,
    )
    client = FakeClient(model_infos={
        "small-model": {"state": "loaded", "loaded_context_length": 8192,
                        "max_context_length": 32768},
    })
    agent = CharlieAgent(client)
    budget = await agent._resolve_input_budget("small-model", override=None)
    # 8192 - 4000 reserve - 500 system ~= 3692, capped by CHARLIE_INPUT_TOKEN_LIMIT
    expected_max = 8192 - CHARLIE_OUTPUT_RESERVE_TOKENS - 500
    assert 0 < budget <= expected_max
    assert budget < 8192


async def test_resolve_input_budget_falls_back_when_model_info_missing():
    """If /api/v0/models is unavailable (older LM Studio or unknown model id),
    we fall back to CHARLIE_FALLBACK_CONTEXT instead of crashing."""
    from round_robin.charlie.agent import CHARLIE_FALLBACK_CONTEXT, CHARLIE_OUTPUT_RESERVE_TOKENS
    client = FakeClient(model_infos={})  # no entry → model_info returns None
    agent = CharlieAgent(client)
    budget = await agent._resolve_input_budget("unknown-model", override=None)
    assert budget > 0
    assert budget <= CHARLIE_FALLBACK_CONTEXT - CHARLIE_OUTPUT_RESERVE_TOKENS


async def test_resolve_input_budget_uses_max_when_not_loaded():
    """Model present in /api/v0/models but not yet loaded → use max_context_length."""
    from round_robin.charlie.agent import CHARLIE_OUTPUT_RESERVE_TOKENS
    client = FakeClient(model_infos={
        "lazy-model": {"state": "not-loaded", "max_context_length": 16384},
    })
    agent = CharlieAgent(client)
    budget = await agent._resolve_input_budget("lazy-model", override=None)
    assert 0 < budget <= 16384 - CHARLIE_OUTPUT_RESERVE_TOKENS - 500


async def test_summarize_uses_real_context_when_available():
    """End-to-end: summarize() with a model whose loaded_context_length is small
    forces truncation even when the user's CHARLIE_INPUT_TOKEN_LIMIT is generous."""
    client = FakeClient(
        chat_response=SAMPLE_BODY,
        model_infos={
            "tiny-ctx": {"state": "loaded", "loaded_context_length": 2048,
                         "max_context_length": 32768},
        },
    )
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    events, emit = collect_events()

    # 50 turns × ~400 chars each = ~20k chars = ~5k tokens — far over 2048's budget
    transcript = [{"agent": "orchestrator", "content": "Theme: t"}]
    for i in range(50):
        transcript.append({
            "agent": "Alpha" if i % 2 == 0 else "Bravo",
            "content": f"turn{i} " + ("x" * 400),
        })

    await agent.summarize(
        workspace=ws, transcript=transcript, theme="t",
        model="tiny-ctx", run_id="r-real", emit=emit,
    )

    done = next(e[1] for e in events if e[0] == "charlie_done")
    # Real budget kicked in → must have truncated.
    assert done["truncated"] is True
    assert done["dropped_turns"] > 0

    # The "budgeting" progress event reports the resolved budget — should be
    # smaller than the full prompt yet > 0.
    budgeting = next(e[1] for e in events
                     if e[0] == "charlie_progress" and e[1].get("phase") == "budgeting")
    assert 0 < budgeting["input_budget"] < 2048


# ── last_error surfacing ─────────────────────────────────────────────────────


async def test_summarize_records_last_error_on_busy():
    client = FakeClient(chat_response=SAMPLE_BODY)
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    _, emit = collect_events()

    agent._busy = True
    path = await agent.summarize(
        workspace=ws, transcript=[{"agent": "Alpha", "content": "x"}],
        theme="t", model="m", run_id="r", emit=emit,
    )
    assert path is None
    assert agent.last_error
    assert "still summarizing" in agent.last_error


async def test_summarize_records_last_error_on_empty_response():
    client = FakeClient(chat_response="   ")
    agent = CharlieAgent(client)
    ws = CharlieWorkspace()
    _, emit = collect_events()

    path = await agent.summarize(
        workspace=ws, transcript=[{"agent": "Alpha", "content": "x"}],
        theme="t", model="m", run_id="r", emit=emit,
    )
    assert path is None
    assert agent.last_error
    assert "empty response" in agent.last_error


async def test_summarize_clears_last_error_on_success():
    client = FakeClient(chat_response=SAMPLE_BODY)
    agent = CharlieAgent(client)
    agent.last_error = "stale"  # leftover from a prior run
    ws = CharlieWorkspace()
    _, emit = collect_events()

    path = await agent.summarize(
        workspace=ws,
        transcript=[{"agent": "Alpha", "content": "hi"},
                     {"agent": "Bravo", "content": "yo"}],
        theme="t", model="m", run_id="r", emit=emit,
    )
    assert path == SUMMARY_FILENAME
    assert agent.last_error is None


# ── Per-turn clipping when transcript is small but turns are huge ───────────


def test_truncate_clips_individual_turns_when_under_keep_count():
    """4 turns, each 5000 chars (~1250 tokens). With limit=200 and keep_head=2,
    keep_tail=6, no whole turns get dropped (4 ≤ 8) — but each turn must be
    clipped so total fits. This is the exact case that produced the 21:11
    `n_keep: 6766 >= n_ctx: 4096` crash in the wild."""
    from round_robin.charlie.agent import _truncate_transcript, _approx_tokens

    transcript = [
        {"agent": "orchestrator", "content": "Theme: t"},
        *[
            {"agent": "Alpha" if i % 2 == 0 else "Bravo",
             "content": f"turn{i}_start " + ("x" * 5000) + " turn{i}_end".replace("{i}", str(i))}
            for i in range(4)
        ],
    ]
    out, dropped = _truncate_transcript(transcript, token_limit=200,
                                          keep_head=2, keep_tail=6)
    assert dropped == 0  # No whole turns dropped (4 ≤ 8)
    agent_entries = [e for e in out if e.get("agent") in ("Alpha", "Bravo")]
    assert len(agent_entries) == 4  # All 4 still present, just clipped

    # Total tokens after clipping must respect the budget (with some slack
    # for the elision marker overhead — per-turn = 200/4 = 50 tokens each).
    total = sum(_approx_tokens(e["content"]) for e in agent_entries)
    assert total <= 400, f"clipped total {total} should fit in roughly token_limit"

    # Each turn shows the elision marker
    for e in agent_entries:
        assert "chars elided" in e["content"], f"missing elision marker in turn: {e['content'][:100]}"
    # Head + tail of each turn preserved (start AND end markers visible)
    assert any("turn0_start" in e["content"] for e in agent_entries)
    assert any("turn3_end" in e["content"] for e in agent_entries)


def test_truncate_combines_drop_and_clip_for_many_huge_turns():
    """20 huge turns: stage-1 drops middle (down to 8), stage-2 clips each kept
    one further if still over budget."""
    from round_robin.charlie.agent import _truncate_transcript, _approx_tokens

    transcript = [
        {"agent": "orchestrator", "content": "Theme: t"},
        *[
            {"agent": "Alpha" if i % 2 == 0 else "Bravo",
             "content": f"t{i} " + ("y" * 4000)}
            for i in range(20)
        ],
    ]
    out, dropped = _truncate_transcript(transcript, token_limit=300,
                                         keep_head=2, keep_tail=6)
    assert dropped == 12  # 20 - 8 kept
    agent_entries = [e for e in out if e.get("agent") in ("Alpha", "Bravo")]
    assert len(agent_entries) == 8

    total = sum(_approx_tokens(e["content"]) for e in agent_entries)
    # Each kept turn was ~1000 tokens, total was 8000. Per-turn budget here is
    # 300/8 = 37 tokens; allow slack for the elision marker (~10 tokens each)
    # on top. Point: clipping kicked in, not exact accounting.
    assert total < 2000, f"after both stages, total {total} should be far below original ~8000"
    # And clipped to roughly the budget × small constant — never anywhere near original.
    for e in agent_entries:
        assert "chars elided" in e["content"]


def test_truncate_no_clip_when_short_turns_already_fit():
    """A normal short transcript whose turns are tiny: no clipping, no marker."""
    from round_robin.charlie.agent import _truncate_transcript

    transcript = [
        {"agent": "orchestrator", "content": "Theme: t"},
        {"agent": "Alpha", "content": "short reply one"},
        {"agent": "Bravo", "content": "short reply two"},
    ]
    out, dropped = _truncate_transcript(transcript, token_limit=10000)
    assert dropped == 0
    assert out is transcript  # unchanged ref
    agent_entries = [e for e in out if e.get("agent") in ("Alpha", "Bravo")]
    for e in agent_entries:
        assert "chars elided" not in e["content"]
