"""Stage-level tests for the Tester (v0.4).

The Tester's responsibilities:

  1. For each layer in ``ctx["artifacts_by_layer"]``, generate test files
     via the LLM and record them per layer.
  2. Detect a runner (pytest / vitest / jest / shellcheck) and execute
     the tests in a sandboxed temp workspace with a 30-second timeout.
  3. On failed/errored, invoke the matching layer generator from
     ``LAYER_GENERATORS`` *exactly once per layer per build* (separate
     budget from the Reviewer's). Re-run tests; accept the second result.
  4. Record per-layer outcomes in ``ctx["test_results"]`` and publish
     ``STAGE_PROGRESS`` events.

Almost all tests monkeypatch ``run_tests`` to return canned results so
we don't actually spawn subprocess pytest invocations — those are slow,
flaky, and beside the point for stage-level logic. One ``@pytest.mark.slow``
integration test exercises the real subprocess path end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.stages import _runner as runner_module
from development.stages import tester as tester_module
from development.stages.tester import (
    RETRY_REMINDER,
    SYSTEM_PROMPT,
    TesterStage,
)
from development.types import STAGE_PROGRESS, BuildRequest

from tests.conftest import FakeLMClient


# ── helpers ─────────────────────────────────────────────────────────


def _tests_json(files: dict[str, str]) -> str:
    """JSON-encode a {test_path: test_content} test-file map."""
    return json.dumps(files)


def _ctx(
    *,
    artifacts: dict[str, dict[str, str]] | None = None,
    plan_layers: list[dict[str, Any]] | None = None,
    board: MessageBoard | None = None,
    seed_loopbacks: set[str] | None = None,
    seed_reviewer_loopbacks: set[str] | None = None,
) -> dict[str, Any]:
    nested = dict(artifacts or {})
    flat: dict[str, str] = {
        path: content
        for files in nested.values()
        for path, content in files.items()
    }
    out: dict[str, Any] = {
        "build_request": BuildRequest(goal="x"),
        "plan": {"layers": plan_layers or []},
        "artifacts": flat,
        "artifacts_by_layer": nested,
        "message_board": board,
    }
    if seed_loopbacks is not None:
        out["_tester_loopbacks"] = set(seed_loopbacks)
    if seed_reviewer_loopbacks is not None:
        out["_reviewer_loopbacks"] = set(seed_reviewer_loopbacks)
    return out


def _canned_result(
    status: str = "passed",
    *,
    num_passed: int = 1,
    num_failed: int = 0,
    duration_ms: int = 12,
) -> "runner_module.RunnerResult":
    return runner_module.RunnerResult(
        status=status,
        duration_ms=duration_ms,
        stdout_tail="ok" if status == "passed" else "boom",
        stderr_tail="" if status == "passed" else "trace",
        num_passed=num_passed,
        num_failed=num_failed,
    )


def _patch_runner(
    monkeypatch,
    *,
    detect: str | None = "pytest",
    results: list["runner_module.RunnerResult"] | None = None,
) -> list[tuple[Path, str]]:
    """Patch detect_runner + run_tests; return a log of run_tests calls."""
    runs: list[tuple[Path, str]] = []
    queue = list(results or [_canned_result()])

    def fake_detect(workspace, layer_name, files):
        return detect

    async def fake_run(workspace, runner, *, timeout_s=30.0):
        runs.append((workspace, runner))
        if not queue:
            return _canned_result()
        return queue.pop(0)

    monkeypatch.setattr(tester_module.runner_module, "detect_runner", fake_detect)
    monkeypatch.setattr(tester_module.runner_module, "run_tests", fake_run)
    return runs


# ── happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passes_record_test_results_and_skip_loopback(monkeypatch):
    """A passing layer populates test_results with no loopback."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})
    runs = _patch_runner(monkeypatch, detect="pytest")

    fake = FakeLMClient(responses=[_tests_json({"test_app.py": "def test_ok(): pass"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "def f(): return 1"}},
        plan_layers=[{"name": "backend", "purpose": "rest api"}],
    )
    out = await stage.run(ctx)

    assert "backend" in out["test_results"]
    assert out["test_results"]["backend"]["status"] == "passed"
    assert out["test_results"]["backend"]["num_passed"] == 1
    assert out["test_results"]["backend"]["regenerated"] is False
    # Exactly one LLM test-gen call, exactly one runner call.
    assert len(fake.calls) == 1
    assert len(runs) == 1
    # System prompt sent verbatim.
    assert fake.calls[0]["messages"][0]["content"] == SYSTEM_PROMPT
    # Loopback set is empty.
    assert out["_tester_loopbacks"] == set()


@pytest.mark.asyncio
async def test_frontend_layer_uses_vitest_detection(monkeypatch):
    """detect_runner='vitest' is what the Tester acts on for JS layers."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})
    runs = _patch_runner(monkeypatch, detect="vitest")

    fake = FakeLMClient(responses=[_tests_json({"app.test.js": "import {} from 'vitest';"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"frontend": {
            "package.json": json.dumps({"devDependencies": {"vitest": "^1.0"}}),
            "app.js": "export const f = () => 1;",
        }},
        plan_layers=[{"name": "frontend", "purpose": "ui"}],
    )
    out = await stage.run(ctx)

    assert out["test_results"]["frontend"]["status"] == "passed"
    assert out["test_results"]["frontend"]["runner"] == "vitest"
    assert len(runs) == 1 and runs[0][1] == "vitest"


# ── loopback paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_triggers_one_loopback(monkeypatch):
    """failed status → regenerate the layer once → re-run; second result is recorded."""
    regenerated = {"app.py": "def f(): return 42  # fixed"}
    gen_calls: list[dict[str, Any]] = []

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        gen_calls.append({"plan": plan, "layer": layer_obj, "feedback": feedback})
        return regenerated

    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": fake_gen})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[
            _canned_result("failed", num_passed=0, num_failed=2),
            _canned_result("passed", num_passed=2, num_failed=0),
        ],
    )

    fake = FakeLMClient(responses=[_tests_json({"test_app.py": "def test(): pass"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "def f(): return 0"}},
        plan_layers=[{"name": "backend", "purpose": "rest api"}],
    )
    out = await stage.run(ctx)

    # Generator called exactly once with feedback.
    assert len(gen_calls) == 1
    assert gen_calls[0]["feedback"], "expected non-empty feedback issues"
    # Final result is the post-regen pass.
    assert out["test_results"]["backend"]["status"] == "passed"
    assert out["test_results"]["backend"]["regenerated"] is True
    # Layer marked as regenerated by the Tester.
    assert "backend" in out["_tester_loopbacks"]
    # Artifacts replaced with the regenerated version (and flat view too).
    assert out["artifacts_by_layer"]["backend"] == regenerated
    assert out["artifacts"]["app.py"] == "def f(): return 42  # fixed"


@pytest.mark.asyncio
async def test_bounded_loopback_accepts_second_failure(monkeypatch, caplog):
    """After one regen, a second failure is accepted as-is — no third try."""

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        return {"app.py": "still buggy"}

    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": fake_gen})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[
            _canned_result("failed", num_passed=0, num_failed=1),
            _canned_result("failed", num_passed=0, num_failed=1),
        ],
    )

    fake = FakeLMClient(responses=[_tests_json({"test_x.py": "def test(): pass"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    # Final status is the second failure.
    assert out["test_results"]["backend"]["status"] == "failed"
    assert out["test_results"]["backend"]["regenerated"] is True
    assert "backend" in out["_tester_loopbacks"]


@pytest.mark.asyncio
async def test_runner_unavailable_is_recorded_and_continues(monkeypatch):
    """No runner installed → status='runner_unavailable', build continues."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})
    _patch_runner(
        monkeypatch,
        detect=None,  # detect_runner returns None
        results=[],   # run_tests should not be called
    )

    fake = FakeLMClient(responses=[_tests_json({"weird.txt": "no runner for this"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"deployment": {"deploy.yaml": "kind: Deployment"}},
        plan_layers=[{"name": "deployment", "purpose": "k8s"}],
    )
    out = await stage.run(ctx)

    assert out["test_results"]["deployment"]["status"] == "runner_unavailable"
    assert out["test_results"]["deployment"]["regenerated"] is False
    # No regen attempt was made for runner_unavailable.
    assert "deployment" not in out["_tester_loopbacks"]


@pytest.mark.asyncio
async def test_subprocess_timeout_recorded_and_continues(monkeypatch):
    """status='timeout' is recorded; treated as terminal (no regen retry)."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": _never_called_gen})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[_canned_result("timeout", num_passed=-1, num_failed=-1)],
    )

    fake = FakeLMClient(responses=[_tests_json({"test_a.py": "x"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "while True: pass"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
    )
    out = await stage.run(ctx)

    assert out["test_results"]["backend"]["status"] == "timeout"
    assert out["test_results"]["backend"]["regenerated"] is False
    # No loopback for timeouts.
    assert "backend" not in out["_tester_loopbacks"]


async def _never_called_gen(*args, **kwargs):
    raise AssertionError("regen should not be called for timeout/runner_unavailable")


# ── error handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_test_gen_falls_back_to_errored_per_layer(monkeypatch):
    """Two unparseable test-gen responses for one layer → that layer errored;
    other layers continue to run cleanly."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})
    _patch_runner(monkeypatch, detect="pytest")

    fake = FakeLMClient(
        responses=[
            "totally not json",                       # layer1: first attempt
            "still nonsense",                         # layer1: retry
            _tests_json({"test_b.py": "def test():pass"}),  # layer2
        ]
    )
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={
            "backend": {"a.py": "x"},
            "frontend": {"b.html": "y"},
        },
        plan_layers=[
            {"name": "backend", "purpose": "p"},
            {"name": "frontend", "purpose": "q"},
        ],
    )
    out = await stage.run(ctx)

    assert out["test_results"]["backend"]["status"] == "errored"
    assert out["test_results"]["frontend"]["status"] == "passed"
    # Retry used the strict reminder for the failing layer.
    assert fake.calls[1]["messages"][-1]["content"] == RETRY_REMINDER


# ── ctx-shape + event-publishing tests ──────────────────────────────


@pytest.mark.asyncio
async def test_no_artifacts_returns_empty_test_results(monkeypatch, tmp_board):
    """Tester with no artifacts → ctx['test_results'] is empty, no LLM calls."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})
    _patch_runner(monkeypatch, detect="pytest")

    fake = FakeLMClient(responses=[])
    stage = TesterStage(fake)

    ctx = _ctx(artifacts={}, plan_layers=[], board=tmp_board)
    out = await stage.run(ctx)

    assert out["test_results"] == {}
    assert len(fake.calls) == 0
    progress = [e for e in tmp_board.recent(limit=20) if e.kind == STAGE_PROGRESS]
    assert progress == []


@pytest.mark.asyncio
async def test_test_results_populated_across_multiple_layers(monkeypatch):
    """Multiple layers each get their own test_results entry."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[
            _canned_result("passed", num_passed=3),
            _canned_result("passed", num_passed=1),
        ],
    )

    fake = FakeLMClient(
        responses=[
            _tests_json({"test_a.py": "x"}),
            _tests_json({"test_b.py": "y"}),
        ]
    )
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={
            "backend": {"a.py": "1"},
            "database": {"db.py": "2"},
        },
        plan_layers=[
            {"name": "backend", "purpose": "p"},
            {"name": "database", "purpose": "q"},
        ],
    )
    out = await stage.run(ctx)

    assert set(out["test_results"].keys()) == {"backend", "database"}
    assert out["test_results"]["backend"]["num_passed"] == 3
    assert out["test_results"]["database"]["num_passed"] == 1


@pytest.mark.asyncio
async def test_stage_progress_events_have_correct_shape(monkeypatch, tmp_board):
    """STAGE_PROGRESS payloads carry stage/layer/status/counts/runner/regenerated."""

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        return {"a.py": "fixed"}

    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": fake_gen})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[
            _canned_result("failed", num_passed=0, num_failed=1),
            _canned_result("passed", num_passed=1),
            _canned_result("passed", num_passed=2),
        ],
    )

    fake = FakeLMClient(
        responses=[
            _tests_json({"test_a.py": "x"}),
            _tests_json({"test_b.py": "y"}),
        ]
    )
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={
            "backend": {"a.py": "x"},
            "frontend": {"b.html": "<html/>"},
        },
        plan_layers=[
            {"name": "backend", "purpose": "p"},
            {"name": "frontend", "purpose": "q"},
        ],
        board=tmp_board,
    )
    await stage.run(ctx)

    progress = [
        e for e in reversed(tmp_board.recent(limit=20))
        if e.kind == STAGE_PROGRESS
    ]
    assert len(progress) == 2

    backend_evt = next(p for p in progress if p.payload["layer"] == "backend")
    assert backend_evt.payload["stage"] == "tester"
    assert backend_evt.payload["status"] == "passed"
    assert backend_evt.payload["num_passed"] == 1
    assert backend_evt.payload["regenerated"] is True
    assert backend_evt.payload["runner"] == "pytest"

    frontend_evt = next(p for p in progress if p.payload["layer"] == "frontend")
    assert frontend_evt.payload["status"] == "passed"
    assert frontend_evt.payload["regenerated"] is False


@pytest.mark.asyncio
async def test_tester_loopback_independent_of_reviewer_loopback(monkeypatch):
    """A layer regenerated by the Reviewer can STILL be regenerated by the Tester.

    Pre-seed ``_reviewer_loopbacks`` with 'backend' (simulating the
    Reviewer having already used its budget), then run Tester. Tester
    must still be able to regenerate 'backend' on test failure since
    its budget is independent.
    """
    gen_calls: list[Any] = []

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        gen_calls.append(layer_obj.get("name"))
        return {"a.py": "tester-fixed"}

    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": fake_gen})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[
            _canned_result("failed", num_passed=0, num_failed=1),
            _canned_result("passed", num_passed=1),
        ],
    )

    fake = FakeLMClient(responses=[_tests_json({"test_a.py": "x"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"a.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
        seed_reviewer_loopbacks={"backend"},  # Reviewer has used its budget
    )
    out = await stage.run(ctx)

    # Tester regenerated even though Reviewer already had.
    assert gen_calls == ["backend"]
    assert out["_tester_loopbacks"] == {"backend"}
    # Reviewer's set is left alone — independent budgets.
    assert out["_reviewer_loopbacks"] == {"backend"}
    assert out["test_results"]["backend"]["status"] == "passed"


@pytest.mark.asyncio
async def test_pre_seeded_tester_loopback_skips_regen(monkeypatch):
    """Resume scenario: orchestrator pre-seeds _tester_loopbacks → no regen."""
    gen_called = False

    async def fake_gen(*args, **kwargs):
        nonlocal gen_called
        gen_called = True
        return {}

    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": fake_gen})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[_canned_result("failed", num_passed=0, num_failed=1)],
    )

    fake = FakeLMClient(responses=[_tests_json({"test_a.py": "x"})])
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"a.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
        seed_loopbacks={"backend"},
    )
    out = await stage.run(ctx)

    assert gen_called is False
    assert out["test_results"]["backend"]["regenerated"] is False
    assert out["test_results"]["backend"]["status"] == "failed"


@pytest.mark.asyncio
async def test_workspace_cleaned_up_after_run(monkeypatch):
    """No temp dirs leaked: every TestWorkspace path no longer exists post-run."""
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})

    seen_paths: list[Path] = []

    def fake_detect(workspace, layer_name, files):
        seen_paths.append(Path(workspace))
        return "pytest"

    async def fake_run(workspace, runner, *, timeout_s=30.0):
        return _canned_result("passed", num_passed=1)

    monkeypatch.setattr(tester_module.runner_module, "detect_runner", fake_detect)
    monkeypatch.setattr(tester_module.runner_module, "run_tests", fake_run)

    fake = FakeLMClient(
        responses=[
            _tests_json({"test_a.py": "1"}),
            _tests_json({"test_b.py": "2"}),
        ]
    )
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={
            "backend": {"a.py": "x"},
            "frontend": {"b.html": "y"},
        },
        plan_layers=[
            {"name": "backend", "purpose": "p"},
            {"name": "frontend", "purpose": "q"},
        ],
    )
    await stage.run(ctx)

    assert len(seen_paths) == 2
    for p in seen_paths:
        assert not p.exists(), f"temp workspace leaked: {p}"


@pytest.mark.asyncio
async def test_buildresult_test_results_visible_in_serialized_output(
    fake_lm: FakeLMClient, tmp_board: MessageBoard, monkeypatch
):
    """End-to-end: Tester populates test_results, BuildResult.to_dict surfaces it."""
    from development.orchestrator import Orchestrator
    from development.stages import (
        ArchitectStage,
        CoderStage,
    )
    from development.stages import coder as coder_module

    async def gen_backend(plan, layer, llm):
        return {"app.py": "def f(): return 1"}

    monkeypatch.setattr(coder_module, "LAYER_GENERATORS", {"backend": gen_backend})
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {"backend": gen_backend})
    _patch_runner(
        monkeypatch,
        detect="pytest",
        results=[_canned_result("passed", num_passed=1)],
    )

    plan_json = json.dumps(
        {
            "stack": {"backend": "fastapi"},
            "layers": [{"name": "backend", "purpose": "api", "files": ["app.py"]}],
            "dependencies": [],
            "constraints_satisfied": {},
        }
    )

    # Reviewer (clean approval) + Tester (test gen) responses.
    fake = FakeLMClient(
        responses=[
            plan_json,                                 # architect
            json.dumps({                               # reviewer verdict
                "approved": True,
                "issues": [],
                "request_regenerate": False,
            }),
            _tests_json({"test_app.py": "def test(): pass"}),  # tester
        ]
    )

    orch = Orchestrator(fake, tmp_board)
    result = await orch.build(BuildRequest(goal="thing"))

    assert "tester" in result.stages_completed
    assert "backend" in result.test_results
    assert result.test_results["backend"]["status"] == "passed"
    # BuildResult.to_dict must include test_results.
    d = result.to_dict()
    assert "test_results" in d
    assert d["test_results"]["backend"]["status"] == "passed"


@pytest.mark.asyncio
async def test_orchestrator_default_pipeline_includes_tester(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    """v0.4 default pipeline must include TesterStage."""
    from development.orchestrator import Orchestrator

    orch = Orchestrator(fake_lm, tmp_board)
    names = [type(s).__name__ for s in orch.stages]
    assert "TesterStage" in names


# ── Real-subprocess integration test (slow) ─────────────────────────


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_pytest_runner_integration(monkeypatch):
    """Spawn a real pytest subprocess against a tiny generated workspace.

    Slow (~1-3s) — only runs when ``pytest -m slow`` is requested. The
    rest of this file uses canned RunnerResult monkeypatches.
    """
    monkeypatch.setattr(tester_module, "LAYER_GENERATORS", {})

    # Real runner module — no patch.
    fake = FakeLMClient(
        responses=[
            _tests_json({
                "test_smoke.py": (
                    "def test_smoke():\n"
                    "    assert 1 + 1 == 2\n"
                ),
            }),
        ]
    )
    stage = TesterStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "X = 1\n"}},
        plan_layers=[{"name": "backend", "purpose": "smoke"}],
    )
    out = await stage.run(ctx)

    res = out["test_results"]["backend"]
    # Either pytest ran and reported a pass, or pytest is unavailable on
    # this CI box (we still want the test to not blow up).
    assert res["status"] in ("passed", "runner_unavailable")
    if res["status"] == "passed":
        assert res["num_passed"] >= 1
        assert res["num_failed"] == 0
