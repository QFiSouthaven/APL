"""Stage-level tests for the Coder.

The Coder consumes ``ctx["plan"]`` and dispatches each declared layer
to a generator from ``development.layers.LAYER_GENERATORS``. We
monkeypatch that registry per test so we don't depend on real LLM
calls — the generator-level behavior is covered separately in
``tests/test_layer_generators.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from development.layers import LAYER_GENERATORS as _REAL_REGISTRY  # noqa: F401
from development.messageboard import MessageBoard
from development.stages import coder as coder_module
from development.stages.coder import CoderStage
from development.types import (
    STAGE_PROGRESS,
    BuildRequest,
    LayerGenerationError,
)

from tests.conftest import FakeLMClient


# Build a fake registry of generators that record their calls so we can
# assert the Coder dispatched correctly without invoking real LLMs.
def _make_fake_generator(returns: dict[str, str]):
    calls: list[dict[str, Any]] = []

    async def gen(plan, layer, llm):  # noqa: ANN001
        calls.append({"plan": plan, "layer": layer, "llm": llm})
        return dict(returns)

    gen.calls = calls  # type: ignore[attr-defined]
    return gen


def _ctx(
    *,
    plan: dict[str, Any] | None = None,
    board: MessageBoard | None = None,
) -> dict[str, Any]:
    return {
        "build_request": BuildRequest(goal="thing"),
        "plan": plan if plan is not None else {},
        "artifacts": {},
        "message_board": board,
    }


# ── plan-presence contract ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_raises_runtime_error_when_plan_missing():
    """No plan → no Coder. The Coder is downstream of the Architect."""
    fake = FakeLMClient()
    stage = CoderStage(fake)
    ctx = _ctx(plan={})  # explicitly empty/falsy
    ctx.pop("plan", None)

    with pytest.raises(RuntimeError) as exc_info:
        await stage.run(ctx)
    assert "plan" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_raises_runtime_error_when_plan_empty_dict():
    """An empty dict is also rejected — the Architect always populates keys."""
    fake = FakeLMClient()
    stage = CoderStage(fake)
    with pytest.raises(RuntimeError):
        await stage.run(_ctx(plan={}))


# ── dispatch ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "layer_name,returns",
    [
        ("backend", {"server.py": "print('be')"}),
        ("frontend", {"index.html": "<html/>"}),
        ("database", {"schema.sql": "CREATE TABLE t(id INT);"}),
        ("deployment", {"Dockerfile": "FROM python:3"}),
    ],
)
@pytest.mark.asyncio
async def test_dispatches_to_correct_generator_per_layer(
    monkeypatch, layer_name, returns
):
    """Each canonical layer name routes to its own generator."""
    gen = _make_fake_generator(returns)
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {layer_name: gen})

    fake = FakeLMClient()
    stage = CoderStage(fake)
    plan = {
        "stack": {"backend": "fastapi"},
        "layers": [{"name": layer_name, "purpose": "p", "language": "py", "files": []}],
    }
    out = await stage.run(_ctx(plan=plan))

    assert len(gen.calls) == 1
    assert gen.calls[0]["llm"] is fake
    assert out["artifacts"] == returns


@pytest.mark.asyncio
async def test_aggregates_artifacts_from_multiple_layers(monkeypatch):
    backend = _make_fake_generator({"server.py": "be"})
    frontend = _make_fake_generator({"index.html": "fe"})
    monkeypatch.setattr(
        coder_module,
        "LAYER_GENERATORS",
        {"backend": backend, "frontend": frontend},
    )

    plan = {
        "layers": [
            {"name": "backend", "files": ["server.py"]},
            {"name": "frontend", "files": ["index.html"]},
        ]
    }
    stage = CoderStage(FakeLMClient())
    out = await stage.run(_ctx(plan=plan))

    assert out["artifacts"] == {"server.py": "be", "index.html": "fe"}
    assert len(backend.calls) == 1
    assert len(frontend.calls) == 1


@pytest.mark.asyncio
async def test_layer_name_match_is_case_insensitive(monkeypatch):
    """Plan can say 'Backend' or 'BACKEND' and still hit the registry."""
    gen = _make_fake_generator({"server.py": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    plan = {"layers": [{"name": "Backend", "files": []}]}
    stage = CoderStage(FakeLMClient())
    out = await stage.run(_ctx(plan=plan))

    assert len(gen.calls) == 1
    assert "server.py" in out["artifacts"]


# ── skip path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_layer_publishes_skip_event_and_continues(
    monkeypatch, tmp_path
):
    """Unknown layers (e.g. 'docs') skip with STAGE_PROGRESS / skipped=True."""
    gen = _make_fake_generator({"server.py": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    board = MessageBoard(tmp_path / "mb.sqlite3")
    try:
        plan = {
            "layers": [
                {"name": "docs", "files": ["README.md"]},
                {"name": "backend", "files": ["server.py"]},
            ]
        }
        stage = CoderStage(FakeLMClient())
        out = await stage.run(_ctx(plan=plan, board=board))

        # Backend ran, docs was skipped.
        assert out["artifacts"] == {"server.py": "x"}

        events = list(reversed(board.recent(limit=10)))
        progress = [e for e in events if e.kind == STAGE_PROGRESS]
        # One skip + one done = 2 progress events.
        assert len(progress) == 2
        skip_evt = next(e for e in progress if e.payload.get("skipped"))
        assert skip_evt.payload["stage"] == "coder"
        assert skip_evt.payload["layer"] == "docs"
        assert skip_evt.payload["skipped"] is True
    finally:
        board.close()


@pytest.mark.asyncio
async def test_progress_event_records_files_generated_count(monkeypatch, tmp_path):
    gen = _make_fake_generator({"a.py": "1", "b.py": "2", "c.py": "3"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    board = MessageBoard(tmp_path / "mb.sqlite3")
    try:
        plan = {"layers": [{"name": "backend", "files": ["a.py", "b.py", "c.py"]}]}
        stage = CoderStage(FakeLMClient())
        await stage.run(_ctx(plan=plan, board=board))

        progress = [e for e in board.recent(10) if e.kind == STAGE_PROGRESS]
        assert len(progress) == 1
        assert progress[0].payload["files_generated"] == 3
        assert progress[0].payload["layer"] == "backend"
    finally:
        board.close()


# ── error propagation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_layer_generation_error_propagates(monkeypatch):
    """If a generator gives up (LayerGenerationError), Coder lets it bubble."""

    async def bad_gen(plan, layer, llm):  # noqa: ANN001
        raise LayerGenerationError("backend", raw_response="garbage")

    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": bad_gen})

    plan = {"layers": [{"name": "backend", "files": []}]}
    stage = CoderStage(FakeLMClient())

    with pytest.raises(LayerGenerationError) as exc_info:
        await stage.run(_ctx(plan=plan))
    assert exc_info.value.layer_name == "backend"


# ── empty / edge cases ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_layers_array_yields_empty_artifacts(monkeypatch):
    """A plan with layers=[] is valid — Coder is a no-op."""
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {})

    plan = {"stack": {}, "layers": []}
    stage = CoderStage(FakeLMClient())
    out = await stage.run(_ctx(plan=plan))

    assert out["artifacts"] == {}


@pytest.mark.asyncio
async def test_non_dict_layer_entry_is_ignored(monkeypatch):
    """A malformed plan entry doesn't crash the Coder."""
    gen = _make_fake_generator({"server.py": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    plan = {"layers": [
        "garbage_string_entry",
        {"name": "backend", "files": []},
        42,
    ]}
    stage = CoderStage(FakeLMClient())
    out = await stage.run(_ctx(plan=plan))

    # Only the one valid entry produced artifacts.
    assert out["artifacts"] == {"server.py": "x"}
    assert len(gen.calls) == 1
