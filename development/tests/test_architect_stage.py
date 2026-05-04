"""Stage-level tests for the Architect."""

from __future__ import annotations

import json

import pytest

from development.stages.architect import (
    RETRY_REMINDER,
    SYSTEM_PROMPT,
    ArchitectStage,
)
from development.types import ArchitectFailedError, BuildRequest

from tests.conftest import FakeLMClient


def _ctx_for(goal: str, **kwargs) -> dict:
    return {
        "build_request": BuildRequest(goal=goal, **kwargs),
        "plan": {},
        "artifacts": {},
        "message_board": None,
    }


@pytest.mark.asyncio
async def test_architect_parses_clean_json():
    plan = {
        "stack": {"backend": "fastapi"},
        "layers": [{"name": "api", "purpose": "x", "language": "py", "files": []}],
        "dependencies": ["fastapi"],
        "constraints_satisfied": {},
    }
    fake = FakeLMClient(responses=[json.dumps(plan)])
    stage = ArchitectStage(fake)

    out = await stage.run(_ctx_for("a notes app"))

    assert out["plan"]["stack"] == {"backend": "fastapi"}
    assert out["plan"]["dependencies"] == ["fastapi"]
    # Exactly one LLM call — no retry needed.
    assert len(fake.calls) == 1
    # System prompt is sent verbatim.
    assert fake.calls[0]["messages"][0]["content"] == SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_architect_strips_code_fences():
    plan = {"stack": {}, "layers": [], "dependencies": []}
    fenced = "```json\n" + json.dumps(plan) + "\n```"
    fake = FakeLMClient(responses=[fenced])
    stage = ArchitectStage(fake)

    out = await stage.run(_ctx_for("hi"))
    assert out["plan"]["stack"] == {}
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_architect_retries_on_garbage_then_succeeds():
    plan = {"stack": {"x": 1}, "layers": [], "dependencies": []}
    fake = FakeLMClient(
        responses=["this is not json at all", json.dumps(plan)]
    )
    stage = ArchitectStage(fake)

    out = await stage.run(_ctx_for("a thing"))

    assert out["plan"]["stack"] == {"x": 1}
    # Two calls — one initial, one retry.
    assert len(fake.calls) == 2
    # Retry uses the strict-reminder body as the last user message.
    second = fake.calls[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"] == RETRY_REMINDER


@pytest.mark.asyncio
async def test_architect_raises_after_retry_failure():
    fake = FakeLMClient(responses=["nope", "still nope"])
    stage = ArchitectStage(fake)

    with pytest.raises(ArchitectFailedError) as exc_info:
        await stage.run(_ctx_for("a thing"))
    assert "still nope" in exc_info.value.raw_response
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_architect_user_prompt_includes_hints_and_constraints():
    fake = FakeLMClient(responses=[json.dumps({"stack": {}, "layers": [], "dependencies": []})])
    stage = ArchitectStage(fake)

    await stage.run(
        _ctx_for(
            "todo app",
            stack_hint="fastapi+sqlite",
            target_lang="python",
            constraints={"max_loc": 200},
        )
    )

    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "Goal: todo app" in user_msg
    assert "fastapi+sqlite" in user_msg
    assert "python" in user_msg
    assert "max_loc" in user_msg


@pytest.mark.asyncio
async def test_architect_normalizes_missing_keys():
    fake = FakeLMClient(responses=[json.dumps({"stack": {}})])  # missing other keys
    stage = ArchitectStage(fake)

    out = await stage.run(_ctx_for("hi"))
    assert out["plan"]["layers"] == []
    assert out["plan"]["dependencies"] == []
    assert out["plan"]["constraints_satisfied"] == {}
