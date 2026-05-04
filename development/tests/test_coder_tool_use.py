"""Tests for the v2.0 Coder ``tool_use=True`` opt-in path.

These tests exercise the tool-loop (LLM ⇄ TOOL_DISPATCH) end-to-end
with a fake LM client that emits canned ``chat_with_tools`` responses.
``tool_use=False`` (default) is regression-guarded against changes to
the v0.5 behavior.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.stages import coder as coder_module
from development.stages.coder import CoderStage
from development.types import (
    STAGE_PROGRESS,
    BuildRequest,
    LayerGenerationError,
)

from tests.conftest import FakeLMClient


class ToolUseFakeLMClient(FakeLMClient):
    """Extension of FakeLMClient that scripts ``chat_with_tools`` responses.

    Construct with ``tool_responses=[dict, ...]``. Each entry is the
    dict returned to the Coder; pop one per ``chat_with_tools`` call.
    Records every ``chat_with_tools`` call in ``self.tool_calls``.
    """

    def __init__(
        self,
        *,
        tool_responses: list[dict] | None = None,
        responses: list[str] | None = None,
        default_model: str = "fake-model",
    ) -> None:
        super().__init__(responses=responses, default_model=default_model)
        self._tool_responses: list[dict] = list(tool_responses or [])
        self.tool_chat_calls: list[dict[str, Any]] = []

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        self.tool_chat_calls.append(
            {"messages": list(messages), "tools": tools, "model": model}
        )
        if not self._tool_responses:
            # Default: empty content, no tool calls. Coder will treat
            # this as a parse failure and raise LayerGenerationError.
            return {"content": "", "tool_calls": []}
        return self._tool_responses.pop(0)


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


def _make_fake_generator(returns: dict[str, str]):
    """Mimics tests/test_coder_stage.py's helper."""
    calls: list[dict[str, Any]] = []

    async def gen(plan, layer, llm):  # noqa: ANN001
        calls.append({"plan": plan, "layer": layer, "llm": llm})
        return dict(returns)

    gen.calls = calls  # type: ignore[attr-defined]
    return gen


# ── default tool_use=False ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_default_false_unchanged_from_v05(monkeypatch):
    """Regression guard: tool_use=False (default) calls the registry generator."""
    gen = _make_fake_generator({"server.py": "print('be')"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    fake = ToolUseFakeLMClient()
    stage = CoderStage(fake)  # tool_use defaults to False

    plan = {"layers": [{"name": "backend", "files": ["server.py"]}]}
    out = await stage.run(_ctx(plan=plan))

    # Generator was used (not chat_with_tools).
    assert len(gen.calls) == 1
    assert out["artifacts"] == {"server.py": "print('be')"}
    # And no tool-aware chat happened.
    assert fake.tool_chat_calls == []


# ── tool_use=True happy path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_true_happy_path_no_tool_calls(monkeypatch):
    """LLM returns final JSON immediately — no tools used."""
    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    final_json = json.dumps({"server.py": "print('hi')"})
    fake = ToolUseFakeLMClient(
        tool_responses=[{"content": final_json, "tool_calls": []}]
    )
    stage = CoderStage(fake, tool_use=True)

    plan = {"layers": [{"name": "backend", "files": ["server.py"]}]}
    out = await stage.run(_ctx(plan=plan))

    assert out["artifacts"] == {"server.py": "print('hi')"}
    # Registry generator NOT called when tool_use=True.
    assert gen.calls == []
    # One tool-aware call happened.
    assert len(fake.tool_chat_calls) == 1


@pytest.mark.asyncio
async def test_tool_use_true_one_tool_call_then_final(monkeypatch):
    """LLM emits one fs_list call, sees the result, then returns final JSON."""
    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    final_json = json.dumps({"server.py": "print('listed')"})
    fake = ToolUseFakeLMClient(
        tool_responses=[
            {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "name": "fs_list", "arguments": {"path": "."}}
                ],
            },
            {"content": final_json, "tool_calls": []},
        ]
    )
    stage = CoderStage(fake, tool_use=True)

    plan = {"layers": [{"name": "backend", "files": ["server.py"]}]}
    out = await stage.run(_ctx(plan=plan))

    # Final answer parsed.
    assert out["artifacts"] == {"server.py": "print('listed')"}
    # Two chat_with_tools calls (one to emit the tool call, one to wrap up).
    assert len(fake.tool_chat_calls) == 2
    # Second call's messages include the tool result envelope.
    second_messages = fake.tool_chat_calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in second_messages)


# ── budget exhaustion ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_budget_exceeded_forces_final(monkeypatch):
    """After tool_call_budget calls, Coder forces a final via chat()."""
    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    # Every tool-aware call requests another fs_list. This would loop
    # forever if the budget didn't kick in.
    looping_call = {
        "content": None,
        "tool_calls": [
            {"id": "c", "name": "fs_list", "arguments": {"path": "."}}
        ],
    }
    final_json = json.dumps({"out.py": "done"})
    fake = ToolUseFakeLMClient(
        # Enough loop responses to blow past budget=2.
        tool_responses=[looping_call, looping_call, looping_call],
        # The forced-final chat() call returns the JSON answer.
        responses=[final_json],
    )
    stage = CoderStage(fake, tool_use=True, tool_call_budget=2)

    plan = {"layers": [{"name": "backend", "files": []}]}
    out = await stage.run(_ctx(plan=plan))

    # Forced-final path produced the artifact.
    assert out["artifacts"] == {"out.py": "done"}
    # The plain chat() force-final fired exactly once.
    assert len(fake.calls) == 1


# ── tool dispatch failure feeds back ───────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_failure_envelope_fed_back_to_llm(monkeypatch):
    """An ok=false tool result is appended as a tool-role message."""
    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    final_json = json.dumps({"server.py": "ok"})
    # The LLM asks fs_read for a path that doesn't exist → ok=False.
    fake = ToolUseFakeLMClient(
        tool_responses=[
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "fs_read",
                        "arguments": {"path": "nope.txt"},
                    }
                ],
            },
            {"content": final_json, "tool_calls": []},
        ]
    )
    stage = CoderStage(fake, tool_use=True)

    plan = {"layers": [{"name": "backend", "files": []}]}
    out = await stage.run(_ctx(plan=plan))
    assert out["artifacts"] == {"server.py": "ok"}

    # The second LLM call must include a tool-role message whose content
    # is JSON containing ok=false / file_not_found.
    second_messages = fake.tool_chat_calls[1]["messages"]
    tool_msgs = [m for m in second_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["ok"] is False
    assert payload["error"] == "file_not_found"


# ── sandboxed_exec actually runs ─────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_sandboxed_exec_runs_and_feeds_back(monkeypatch):
    """A sandboxed_exec tool call really spawns a subprocess and the
    stdout flows back into the messages."""
    import sys

    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    final_json = json.dumps({"server.py": "print('done')"})
    fake = ToolUseFakeLMClient(
        tool_responses=[
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "sandboxed_exec",
                        "arguments": {
                            "cmd": [sys.executable, "-c", "print('SENTINEL')"],
                            "cwd": ".",
                            "timeout_s": 10.0,
                        },
                    }
                ],
            },
            {"content": final_json, "tool_calls": []},
        ]
    )
    stage = CoderStage(fake, tool_use=True)

    plan = {"layers": [{"name": "backend", "files": []}]}
    out = await stage.run(_ctx(plan=plan))
    assert out["artifacts"] == {"server.py": "print('done')"}

    second_messages = fake.tool_chat_calls[1]["messages"]
    tool_msgs = [m for m in second_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["ok"] is True
    assert "SENTINEL" in payload["stdout_tail"]


# ── progress event includes tool_calls_used ─────────────────────────


@pytest.mark.asyncio
async def test_stage_progress_includes_tool_calls_used(monkeypatch, tmp_path):
    """Per-layer STAGE_PROGRESS payload carries tool_calls_used count."""
    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    final_json = json.dumps({"server.py": "ok"})
    fake = ToolUseFakeLMClient(
        tool_responses=[
            {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "name": "fs_list", "arguments": {"path": "."}}
                ],
            },
            {"content": final_json, "tool_calls": []},
        ]
    )
    stage = CoderStage(fake, tool_use=True)

    board = MessageBoard(tmp_path / "mb.sqlite3")
    try:
        plan = {"layers": [{"name": "backend", "files": []}]}
        await stage.run(_ctx(plan=plan, board=board))

        progress = [e for e in board.recent(10) if e.kind == STAGE_PROGRESS]
        assert len(progress) == 1
        payload = progress[0].payload
        assert payload["layer"] == "backend"
        assert payload["files_generated"] == 1
        # New v2.0 field:
        assert "tool_calls_used" in payload
        assert payload["tool_calls_used"] == 1
    finally:
        board.close()


# ── parse-failure guardrail ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_parse_failure_raises_layer_error(monkeypatch):
    """If the final response isn't valid JSON, the Coder raises."""
    gen = _make_fake_generator({"unused": "x"})
    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen})

    fake = ToolUseFakeLMClient(
        tool_responses=[{"content": "not json at all", "tool_calls": []}]
    )
    stage = CoderStage(fake, tool_use=True)

    plan = {"layers": [{"name": "backend", "files": []}]}
    with pytest.raises(LayerGenerationError) as exc_info:
        await stage.run(_ctx(plan=plan))
    assert exc_info.value.layer_name == "backend"
