"""End-to-end tests for the Orchestrator."""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.stages import ArchitectStage
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
    orch = Orchestrator(fake_lm, tmp_board)
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
async def test_default_pipeline_is_just_architect(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    orch = Orchestrator(fake_lm, tmp_board)
    assert [type(s).__name__ for s in orch.stages] == ["ArchitectStage"]


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
