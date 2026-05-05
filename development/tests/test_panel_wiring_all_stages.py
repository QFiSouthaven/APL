"""v2.2 panel wiring across all five stages.

v2.1 wired ``ReasoningPanel`` only into the Reviewer. v2.2 extends the
wiring to Architect, Coder, Tester, and Packager — every stage that
makes a single primary LLM call now routes that call through the panel
when one is supplied via the constructor. Coder's tool-use path is
special: panel involvement is deliberately limited to the FIRST round
per layer (the planning consult before any tool loop) because partner
slots can't coherently emit tool_calls into the shared sandbox.

Each panel-aware stage surfaces telemetry under a stable per-stage key
in the build context:

* ``ctx["architect_panel"]``  — Architect's single panel call
* ``ctx["coder_panel"]``      — last layer's planning consult
* ``ctx["review"][layer]["panel"]`` — Reviewer's per-layer call (v2.1)
* ``ctx["tester_panel"]``     — last layer's test-gen panel call
* ``ctx["packager_panel"]``   — Packager's single panel call

The shape mirrors Reviewer's existing ``{primary, partners: [...]}``
contract so observers can render every stage's panel output uniformly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.reasoning_panel import LLMSlot, ReasoningPanel
from development.stages import (
    ArchitectStage,
    CoderStage,
    PackagerStage,
    ReviewerStage,
    TesterStage,
)
from development.stages import coder as coder_module
from development.stages import packager as packager_module
from development.stages import tester as tester_module
from development.types import BuildRequest

from tests.conftest import FakeLMClient


# ── helpers (mirror test_reasoning_panel_wiring.py idioms) ─────────


def _arch_plan_json() -> str:
    return json.dumps(
        {
            "stack": {"backend": "python", "database": "sqlite"},
            "layers": [
                {
                    "name": "backend",
                    "purpose": "rest api",
                    "language": "python",
                    "files": ["server.py"],
                }
            ],
            "dependencies": ["fastapi"],
            "constraints_satisfied": {},
        }
    )


def _file_map_json(files: dict[str, str]) -> str:
    return json.dumps(files)


def _packaging_files_json() -> str:
    return json.dumps(
        {
            "Dockerfile": (
                "FROM python:3.12-slim AS build\n"
                "FROM python:3.12-slim\n"
                "WORKDIR /app\n"
                "EXPOSE 8000\n"
                "HEALTHCHECK CMD curl -f http://localhost:8000/health\n"
                "CMD [\"python\", \"server.py\"]\n"
            ),
            "docker-compose.yml": (
                "services:\n"
                "  backend:\n"
                "    build: .\n"
                "    ports:\n"
                "      - \"8000:8000\"\n"
            ),
            ".env.example": "DATABASE_URL=\nSECRET_KEY=\n",
            "deploy.sh": "#!/usr/bin/env bash\nset -e\necho deploy\n",
            "deploy.ps1": "Set-StrictMode -Version Latest\n$ErrorActionPreference='Stop'\nWrite-Host deploy\n",
            "README.md": "# project\n",
        }
    )


class _FakePanelProvider:
    """Minimal ChatProvider stand-in matching the reviewer-test idiom."""

    def __init__(
        self,
        responses: list[str],
        *,
        raise_after_n: int | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.raise_after_n = raise_after_n

    async def chat(self, messages, *, model, **kwargs):
        self.calls.append({"messages": list(messages), "model": model, **kwargs})
        if self.raise_after_n is not None and len(self.calls) > self.raise_after_n:
            raise RuntimeError("fake provider crash")
        if not self.responses:
            return ""
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover
        if False:
            yield ""

    async def list_models(self):  # pragma: no cover
        return []


def _slot(
    name: str,
    *,
    responses: list[str],
    weight: float = 1.0,
) -> LLMSlot:
    return LLMSlot(
        name=name,
        provider=_FakePanelProvider(responses),
        model="fake-model",
        role="",
        weight=weight,
    )


def _two_slot_panel(primary_response: str, partner_response: str) -> ReasoningPanel:
    return ReasoningPanel(
        [
            _slot("primary", responses=[primary_response]),
            _slot("partner", responses=[partner_response]),
        ]
    )


# ── 1. Architect uses panel when supplied ──────────────────────────


@pytest.mark.asyncio
async def test_architect_uses_panel_when_supplied():
    """Architect with a panel routes its plan call through panel.consult.

    Both slots' chat() should fire once; ctx["architect_panel"] should
    carry primary + partners with the canonical {primary, partners}
    shape.
    """
    panel = _two_slot_panel(_arch_plan_json(), _arch_plan_json())

    fake = FakeLMClient(responses=[])  # bypassed when panel is wired
    stage = ArchitectStage(fake, reasoning_panel=panel)

    ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="rest api"),
        "plan": {},
        "artifacts": {},
        "message_board": None,
    }
    out = await stage.run(ctx)

    # Plan is populated as if the bare LLM had answered.
    assert "backend" in {layer["name"] for layer in out["plan"]["layers"]}
    # Panel telemetry surfaces under the architect_panel key.
    telemetry = out["architect_panel"]
    assert telemetry["primary"] == _arch_plan_json()
    partner_names = [p["name"] for p in telemetry["partners"]]
    assert partner_names == ["partner"]
    # Both slots got called exactly once.
    primary_slot, partner_slot = panel.slots
    assert len(primary_slot.provider.calls) == 1
    assert len(partner_slot.provider.calls) == 1
    # The bare LLMClient was bypassed entirely.
    assert len(fake.calls) == 0


# ── 2. Coder uses panel for the planning round only ────────────────


class _ToolLoopFakeLM(FakeLMClient):
    """Scripts chat_with_tools: one tool call, then a final JSON answer."""

    def __init__(self, *, final_files: dict[str, str]):
        super().__init__(responses=[])
        self.tool_chat_calls: list[dict[str, Any]] = []
        # Two scripted responses: first emits a tool_call, second
        # returns the final {path: content} JSON.
        self._tool_responses: list[dict[str, Any]] = [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "fs_read",
                        "arguments": {"path": "missing.txt"},
                    }
                ],
            },
            {
                "content": _file_map_json(final_files),
                "tool_calls": [],
            },
        ]

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        self.tool_chat_calls.append({"messages": list(messages)})
        if not self._tool_responses:
            return {"content": "", "tool_calls": []}
        return self._tool_responses.pop(0)


@pytest.mark.asyncio
async def test_coder_uses_panel_for_first_round_only():
    """Coder in tool_use mode calls panel ONCE per layer (planning)
    and then drives the tool loop via the bare provider.

    Panel slots fire once each (planning consult); the subsequent
    chat_with_tools rounds (tool call → final JSON) all go through
    the single-provider path. Telemetry lands in ctx["coder_panel"].
    """
    fake = _ToolLoopFakeLM(final_files={"server.py": "print('hi')"})
    panel = _two_slot_panel("Plan: stub a server.", "Sounds good.")
    stage = CoderStage(fake, tool_use=True, reasoning_panel=panel)

    plan = {"layers": [{"name": "backend", "purpose": "p", "files": ["server.py"]}]}
    ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": plan,
        "artifacts": {},
        "message_board": None,
    }
    out = await stage.run(ctx)

    # Files made it through the tool-loop final JSON.
    assert out["artifacts"] == {"server.py": "print('hi')"}
    # Panel telemetry surfaces.
    telemetry = out["coder_panel"]
    assert "primary" in telemetry
    assert [p["name"] for p in telemetry["partners"]] == ["partner"]
    # Each panel slot was consulted EXACTLY once (the planning round).
    primary_slot, partner_slot = panel.slots
    assert len(primary_slot.provider.calls) == 1
    assert len(partner_slot.provider.calls) == 1
    # The tool loop fired against the bare provider — at least 2 rounds
    # (tool call, then final JSON).
    assert len(fake.tool_chat_calls) >= 2


@pytest.mark.asyncio
async def test_coder_no_panel_unchanged():
    """Without a panel, Coder behavior is byte-for-byte v2.0."""
    fake = _ToolLoopFakeLM(final_files={"server.py": "print('hi')"})
    stage = CoderStage(fake, tool_use=True)  # no panel

    plan = {"layers": [{"name": "backend", "purpose": "p", "files": ["server.py"]}]}
    ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": plan,
        "artifacts": {},
        "message_board": None,
    }
    out = await stage.run(ctx)

    assert out["artifacts"] == {"server.py": "print('hi')"}
    # No panel telemetry key.
    assert "coder_panel" not in out


# ── 3. Tester uses panel when supplied ─────────────────────────────


@pytest.mark.asyncio
async def test_tester_uses_panel_when_supplied(monkeypatch):
    """Tester routes test-generation calls through the panel."""
    # No runner detected → status='runner_unavailable', no subprocess fires.
    monkeypatch.setattr(
        tester_module.runner_module, "detect_runner", lambda *_a, **_kw: None
    )

    test_files = {"test_server.py": "def test_x(): pass"}
    panel = _two_slot_panel(_file_map_json(test_files), _file_map_json(test_files))

    fake = FakeLMClient(responses=[])
    stage = TesterStage(fake, reasoning_panel=panel)

    ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": {"layers": [{"name": "backend", "purpose": "p"}]},
        "artifacts": {"server.py": "x"},
        "artifacts_by_layer": {"backend": {"server.py": "x"}},
        "message_board": None,
    }
    out = await stage.run(ctx)

    # Telemetry surfaces under tester_panel.
    telemetry = out["tester_panel"]
    assert telemetry["primary"] == _file_map_json(test_files)
    assert [p["name"] for p in telemetry["partners"]] == ["partner"]
    # Each slot called once.
    primary_slot, partner_slot = panel.slots
    assert len(primary_slot.provider.calls) == 1
    assert len(partner_slot.provider.calls) == 1
    # Bare LLMClient was bypassed.
    assert len(fake.calls) == 0


# ── 4. Packager uses panel when supplied ───────────────────────────


@pytest.mark.asyncio
async def test_packager_uses_panel_when_supplied():
    """Packager routes its packaging file-gen call through the panel."""
    panel = _two_slot_panel(_packaging_files_json(), _packaging_files_json())

    fake = FakeLMClient(responses=[])
    stage = PackagerStage(fake, reasoning_panel=panel)

    ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": {"stack": {"backend": "python"}, "constraints_satisfied": {}},
        "artifacts": {"server.py": "x"},
        "artifacts_by_layer": {"backend": {"server.py": "x"}},
        "message_board": None,
    }
    out = await stage.run(ctx)

    # Required packaging files landed in artifacts.
    assert "Dockerfile" in out["artifacts"]
    assert "docker-compose.yml" in out["artifacts"]
    # Telemetry under packager_panel.
    telemetry = out["packager_panel"]
    assert telemetry["primary"] == _packaging_files_json()
    assert [p["name"] for p in telemetry["partners"]] == ["partner"]
    # Each slot called once.
    primary_slot, partner_slot = panel.slots
    assert len(primary_slot.provider.calls) == 1
    assert len(partner_slot.provider.calls) == 1
    assert len(fake.calls) == 0


# ── 5. No-panel path leaves ctx unchanged (regression guard) ───────


@pytest.mark.asyncio
async def test_all_stages_panel_none_unchanged(monkeypatch):
    """Without panels, no ``*_panel`` keys appear in ctx (v2.0 contract)."""
    # Architect.
    arch_fake = FakeLMClient(responses=[_arch_plan_json()])
    arch = ArchitectStage(arch_fake)  # no panel
    arch_ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": {},
        "artifacts": {},
        "message_board": None,
    }
    out_arch = await arch.run(arch_ctx)
    assert "architect_panel" not in out_arch

    # Tester.
    monkeypatch.setattr(
        tester_module.runner_module, "detect_runner", lambda *_a, **_kw: None
    )
    test_fake = FakeLMClient(responses=[_file_map_json({"t.py": "x"})])
    tester = TesterStage(test_fake)  # no panel
    test_ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": {"layers": [{"name": "backend", "purpose": "p"}]},
        "artifacts": {},
        "artifacts_by_layer": {"backend": {"server.py": "x"}},
        "message_board": None,
    }
    out_test = await tester.run(test_ctx)
    assert "tester_panel" not in out_test

    # Packager.
    pkg_fake = FakeLMClient(responses=[_packaging_files_json()])
    pkg = PackagerStage(pkg_fake)  # no panel
    pkg_ctx: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": {"stack": {"backend": "python"}, "constraints_satisfied": {}},
        "artifacts": {},
        "artifacts_by_layer": {"backend": {"server.py": "x"}},
        "message_board": None,
    }
    out_pkg = await pkg.run(pkg_ctx)
    assert "packager_panel" not in out_pkg


# ── 6. Orchestrator threads panel into all 5 stages ────────────────


def test_orchestrator_threads_panel_to_all_stages(fake_lm, tmp_board):
    """v2.2: every default-pipeline stage receives the panel via ctor."""
    panel = _two_slot_panel(_arch_plan_json(), _arch_plan_json())
    orch = Orchestrator(fake_lm, tmp_board, reasoning_panel=panel)
    stages = orch.stages
    assert [s.name for s in stages] == [
        "architect", "coder", "reviewer", "tester", "packager",
    ]
    for s in stages:
        assert s._reasoning_panel is panel
