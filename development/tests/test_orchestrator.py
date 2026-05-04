"""End-to-end tests for the Orchestrator."""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.stages import ArchitectStage, CoderStage
from development.stages import coder as coder_module
from development.stages.base import Stage
from development.types import (
    BUILD_DONE,
    BUILD_FAILED,
    BUILD_STARTED,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_STARTED,
    BuildRequest,
)

from tests.conftest import FakeLMClient


@pytest.mark.asyncio
async def test_build_runs_architect_and_emits_events(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    """Default pipeline (v0.3) is Architect → Coder → Reviewer.

    Use a minimal stages=[ArchitectStage] override so this test pins
    the Architect-only event sequence; the full pipeline event sequence
    is covered separately below.
    """
    orch = Orchestrator(
        fake_lm, tmp_board,
        stages=[ArchitectStage(fake_lm)],
    )
    result = await orch.build(BuildRequest(goal="a counter app"))

    assert result.stages_completed == ("architect",)
    assert result.errors == ()
    assert result.duration_ms >= 0
    assert "stack" in result.plan and "layers" in result.plan

    # Verify the event sequence on the board.
    events = list(reversed(tmp_board.recent(limit=10)))  # oldest-first
    kinds = [e.kind for e in events]
    assert kinds == [BUILD_STARTED, STAGE_STARTED, STAGE_DONE, BUILD_DONE]


@pytest.mark.asyncio
async def test_build_failure_publishes_failure_events(tmp_board: MessageBoard):
    """Architect that always returns garbage → ArchitectFailedError → build fail."""
    bad = FakeLMClient(responses=["garbage", "still garbage"])
    orch = Orchestrator(bad, tmp_board)
    result = await orch.build(BuildRequest(goal="x"))

    assert result.stages_completed == ()
    assert result.errors and "architect" in result.errors[0]

    events = list(reversed(tmp_board.recent(limit=10)))
    kinds = [e.kind for e in events]
    assert kinds == [BUILD_STARTED, STAGE_STARTED, STAGE_FAILED, BUILD_FAILED]
    # No BUILD_DONE on a failed build.
    assert BUILD_DONE not in kinds


@pytest.mark.asyncio
async def test_default_pipeline_is_architect_coder_reviewer(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    """v0.3 default: Architect → Coder → Reviewer.

    Pinned because the framework doc commits to this sequence as the
    canonical full-build chain. v0.4+ adds Tester then Packager as
    explicit additions; default-removal is a v3.0 break.
    """
    orch = Orchestrator(fake_lm, tmp_board)
    assert [type(s).__name__ for s in orch.stages] == [
        "ArchitectStage", "CoderStage", "ReviewerStage",
    ]


class _RecordingStage(Stage):
    """Test helper: captures the ctx and returns a marker artifact."""

    name = "recording"

    def __init__(self, llm) -> None:
        super().__init__(llm)
        self.seen_ctx: dict[str, Any] | None = None

    async def run(self, ctx):
        self.seen_ctx = ctx
        ctx.setdefault("artifacts", {})["recording.txt"] = "hello"
        return ctx


@pytest.mark.asyncio
async def test_stages_chain_via_ctx(fake_lm: FakeLMClient, tmp_board: MessageBoard):
    """The Architect's plan must reach a downstream stage through ctx."""
    rec = _RecordingStage(fake_lm)
    orch = Orchestrator(
        fake_lm,
        tmp_board,
        stages=[ArchitectStage(fake_lm), rec],
    )
    result = await orch.build(BuildRequest(goal="thing"))

    assert result.stages_completed == ("architect", "recording")
    assert rec.seen_ctx is not None
    # The plan from the Architect is visible in the second stage's ctx.
    assert "stack" in rec.seen_ctx["plan"]
    # Artifact propagated into the result.
    assert result.artifacts == {"recording.txt": "hello"}


@pytest.mark.asyncio
async def test_failure_in_second_stage_keeps_first_stage_in_completed(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    class _Boom(Stage):
        name = "boom"

        async def run(self, ctx):
            raise RuntimeError("kaboom")

    orch = Orchestrator(
        fake_lm,
        tmp_board,
        stages=[ArchitectStage(fake_lm), _Boom(fake_lm)],
    )
    result = await orch.build(BuildRequest(goal="thing"))

    assert result.stages_completed == ("architect",)
    assert any("boom" in e for e in result.errors)


# ── v0.2 Coder integration ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_coder_runs_after_architect_when_both_succeed(
    monkeypatch, tmp_board: MessageBoard
):
    """Pipeline [Architect, Coder]: plan flows from Arch ctx → Coder ctx."""

    async def fake_backend_gen(plan, layer, llm):
        return {"server.py": "print('ok')"}

    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": fake_backend_gen})

    plan_json = json.dumps(
        {
            "stack": {"backend": "fastapi"},
            "layers": [{"name": "backend", "purpose": "api", "files": ["server.py"]}],
            "dependencies": [],
            "constraints_satisfied": {},
        }
    )
    fake = FakeLMClient(responses=[plan_json])
    orch = Orchestrator(
        fake, tmp_board,
        stages=[ArchitectStage(fake), CoderStage(fake)],
    )

    result = await orch.build(BuildRequest(goal="rest api"))

    assert result.stages_completed == ("architect", "coder")
    assert result.errors == ()
    assert result.artifacts == {"server.py": "print('ok')"}
    assert result.plan["stack"] == {"backend": "fastapi"}


@pytest.mark.asyncio
async def test_coder_does_not_run_when_architect_fails(tmp_board: MessageBoard):
    """If the Architect raises, the Coder must not get a chance to run."""
    coder_ran = False

    class _RecordingCoder(Stage):
        name = "coder"

        async def run(self, ctx):
            nonlocal coder_ran
            coder_ran = True
            return ctx

    bad = FakeLMClient(responses=["garbage", "still garbage"])
    orch = Orchestrator(
        bad,
        tmp_board,
        stages=[ArchitectStage(bad), _RecordingCoder(bad)],
    )
    result = await orch.build(BuildRequest(goal="x"))

    assert coder_ran is False
    assert result.stages_completed == ()
    assert any("architect" in e for e in result.errors)


@pytest.mark.asyncio
async def test_end_to_end_build_produces_artifacts(monkeypatch, tmp_board: MessageBoard):
    """Full happy path: BuildRequest → plan → multi-layer artifacts."""

    async def gen_backend(plan, layer, llm):
        return {"app.py": "# backend"}

    async def gen_frontend(plan, layer, llm):
        return {"index.html": "<html></html>"}

    monkeypatch.setattr(
        coder_module,
        "LAYER_GENERATORS",
        {"backend": gen_backend, "frontend": gen_frontend},
    )

    plan_json = json.dumps(
        {
            "stack": {"backend": "flask", "frontend": "vanilla"},
            "layers": [
                {"name": "backend", "files": ["app.py"]},
                {"name": "frontend", "files": ["index.html"]},
            ],
            "dependencies": [],
            "constraints_satisfied": {},
        }
    )
    fake = FakeLMClient(responses=[plan_json])
    orch = Orchestrator(
        fake,
        tmp_board,
        stages=[ArchitectStage(fake), CoderStage(fake)],
    )

    result = await orch.build(BuildRequest(goal="hello world"))

    assert result.stages_completed == ("architect", "coder")
    assert result.errors == ()
    assert result.artifacts == {"app.py": "# backend", "index.html": "<html></html>"}
    assert result.plan["stack"]["frontend"] == "vanilla"

    # Event sequence ends in BUILD_DONE.
    events = list(reversed(tmp_board.recent(limit=20)))
    kinds = [e.kind for e in events]
    assert kinds[0] == BUILD_STARTED
    assert kinds[-1] == BUILD_DONE
