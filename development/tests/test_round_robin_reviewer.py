"""Tests for the v2.0 Round-Robin reviewer alternate.

Covers the registry (``development.reviewers.get_reviewer``), the
deferred-mode fallback path (no review endpoint on the round-robin peer
yet), the orchestrator's per-build reviewer swap, and the
``BuildRequest.reviewer`` wire-level field.

We never hit a real round-robin peer — every test either monkeypatches
``discovery.get_peer_url`` to look unreachable or stubs the reviewer's
HTTP helpers to assert behavior without httpx round-tripping.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.reviewers import REVIEWERS, RoundRobinReviewer, get_reviewer
from development.reviewers import round_robin as rr_module
from development.stages import reviewer as reviewer_module
from development.stages.reviewer import ReviewerStage
from development.types import STAGE_PROGRESS, BuildRequest

from tests.conftest import FakeLMClient


def _verdict_json(approved: bool, issues: list[str], request_regenerate: bool) -> str:
    return json.dumps(
        {
            "approved": approved,
            "issues": issues,
            "request_regenerate": request_regenerate,
        }
    )


def _ctx(
    *,
    artifacts: dict[str, dict[str, str]] | None = None,
    plan_layers: list[dict[str, Any]] | None = None,
    board: MessageBoard | None = None,
) -> dict[str, Any]:
    return {
        "build_request": BuildRequest(goal="x"),
        "plan": {"layers": plan_layers or []},
        "artifacts": {},
        "artifacts_by_layer": dict(artifacts or {}),
        "message_board": board,
    }


# ── registry ────────────────────────────────────────────────────────


def test_get_reviewer_single_pass_returns_reviewer_stage():
    assert get_reviewer("single-pass") is ReviewerStage


def test_get_reviewer_round_robin_returns_round_robin_reviewer():
    assert get_reviewer("round-robin") is RoundRobinReviewer


def test_get_reviewer_unknown_raises_key_error():
    with pytest.raises(KeyError):
        get_reviewer("does-not-exist-9000")


def test_registry_keys_are_documented_set():
    # If this fails, REVIEWERS grew a new key — update get_reviewer
    # docstring + BuildRequest.reviewer docstring + orchestrator docs.
    assert set(REVIEWERS.keys()) == {"single-pass", "round-robin"}


# ── BuildRequest plumbing ───────────────────────────────────────────


def test_build_request_default_reviewer_is_single_pass():
    req = BuildRequest(goal="x")
    assert req.reviewer == "single-pass"


def test_build_request_to_dict_includes_reviewer_field():
    req = BuildRequest(goal="x")
    d = req.to_dict()
    assert "reviewer" in d
    assert d["reviewer"] == "single-pass"


def test_build_request_to_dict_carries_round_robin_choice():
    req = BuildRequest(goal="x", reviewer="round-robin")
    assert req.to_dict()["reviewer"] == "round-robin"


# ── Reachability fallback ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_peer_falls_back_to_reviewer_stage(monkeypatch):
    """Empty/missing peer URL → delegate to ReviewerStage, mark deferred."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})
    # Empty discovery URL → reachability fails immediately, no httpx call.
    monkeypatch.setattr(rr_module, "get_peer_url", lambda name: "")

    fake = FakeLMClient(responses=[_verdict_json(True, [], False)])
    stage = RoundRobinReviewer(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "print('hi')"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    # ReviewerStage handled the layer end-to-end — its single LLM critique
    # is the only call we should have seen.
    assert len(fake.calls) == 1
    assert out["review"]["backend"]["approved"] is True
    # The deferred sentinel marks that round-robin never ran.
    assert out["review_source"] == "round-robin-deferred"


@pytest.mark.asyncio
async def test_health_probe_failure_falls_back_to_reviewer_stage(monkeypatch):
    """Health probe returns False (peer down) → delegate to ReviewerStage."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})
    monkeypatch.setattr(rr_module, "get_peer_url", lambda name: "http://127.0.0.1:8766")

    async def fake_is_alive(self, url):
        return False

    monkeypatch.setattr(RoundRobinReviewer, "_is_alive", fake_is_alive)

    fake = FakeLMClient(responses=[_verdict_json(True, [], False)])
    stage = RoundRobinReviewer(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    assert out["review_source"] == "round-robin-deferred"
    assert out["review"]["backend"]["approved"] is True


# ── Deferred-mode endpoint missing ─────────────────────────────────


@pytest.mark.asyncio
async def test_deferred_mode_emits_stage_progress_and_falls_back(monkeypatch, tmp_board):
    """Peer up but /api/review 404s → per-layer fallback + deferred event."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})
    monkeypatch.setattr(rr_module, "get_peer_url", lambda name: "http://127.0.0.1:8766")

    async def fake_is_alive(self, url):
        return True

    async def fake_post_review(self, url, layer_name, layer_obj, files):
        # Simulate the 404 path — endpoint not implemented yet.
        return {}, True

    monkeypatch.setattr(RoundRobinReviewer, "_is_alive", fake_is_alive)
    monkeypatch.setattr(RoundRobinReviewer, "_post_review", fake_post_review)

    fake = FakeLMClient(responses=[_verdict_json(True, [], False)])
    stage = RoundRobinReviewer(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
        board=tmp_board,
    )
    out = await stage.run(ctx)

    # Single-pass took over for this layer.
    assert len(fake.calls) == 1
    assert out["review"]["backend"]["approved"] is True
    assert out["review_source"] == "round-robin-deferred"

    # The exact deferred-mode event payload is documented in the v2.0
    # spec. Keep this assertion strict so observers can rely on the
    # shape.
    deferred_events = [
        e for e in tmp_board.recent(limit=20)
        if e.kind == STAGE_PROGRESS and e.payload.get("deferred")
    ]
    assert len(deferred_events) == 1
    payload = deferred_events[0].payload
    assert payload["stage"] == "reviewer"
    assert payload["layer"] == "backend"
    assert payload["deferred"] is True
    assert payload["reason"] == "no_review_endpoint"
    assert payload["round_robin_url"] == "http://127.0.0.1:8766"


@pytest.mark.asyncio
async def test_round_robin_endpoint_success_sets_review_source(monkeypatch):
    """When /api/review actually responds, review_source = 'round-robin'."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})
    monkeypatch.setattr(rr_module, "get_peer_url", lambda name: "http://127.0.0.1:8766")

    async def fake_is_alive(self, url):
        return True

    async def fake_post_review(self, url, layer_name, layer_obj, files):
        # Simulate a real round-robin verdict coming back.
        return (
            {
                "approved": True,
                "issues": [],
                "request_regenerate": False,
            },
            False,
        )

    monkeypatch.setattr(RoundRobinReviewer, "_is_alive", fake_is_alive)
    monkeypatch.setattr(RoundRobinReviewer, "_post_review", fake_post_review)

    fake = FakeLMClient(responses=[])  # never called — round-robin handled it
    stage = RoundRobinReviewer(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    assert out["review"]["backend"]["approved"] is True
    assert out["review_source"] == "round-robin"
    assert len(fake.calls) == 0


# ── Empty-artifacts edge case ──────────────────────────────────────


@pytest.mark.asyncio
async def test_no_artifacts_returns_empty_review(monkeypatch):
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})
    monkeypatch.setattr(rr_module, "get_peer_url", lambda name: "")

    fake = FakeLMClient(responses=[])
    stage = RoundRobinReviewer(fake)

    ctx = _ctx(artifacts={}, plan_layers=[])
    out = await stage.run(ctx)

    assert out["review"] == {}
    assert out["review_source"] == "round-robin-deferred"
    assert len(fake.calls) == 0


# ── Orchestrator per-build dispatch ────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_swaps_in_round_robin_reviewer(tmp_board, monkeypatch):
    """``BuildRequest(reviewer="round-robin")`` → RoundRobinReviewer in pipeline."""
    fake = FakeLMClient(responses=[])
    orch = Orchestrator(fake, tmp_board)

    # Spy on which class actually got constructed for the build.
    seen: list[type] = []

    original_run = RoundRobinReviewer.run
    original_reviewer_run = ReviewerStage.run

    async def spy_rr_run(self, ctx):
        seen.append(RoundRobinReviewer)
        # Short-circuit: just set a sentinel and return without LLM calls.
        ctx["review"] = {}
        ctx["review_source"] = "round-robin-deferred"
        return ctx

    async def spy_rs_run(self, ctx):
        seen.append(ReviewerStage)
        ctx["review"] = {}
        return ctx

    monkeypatch.setattr(RoundRobinReviewer, "run", spy_rr_run)
    monkeypatch.setattr(ReviewerStage, "run", spy_rs_run)

    # Stub the other stages to no-ops so the build runs end-to-end fast.
    from development.stages import (
        architect as architect_module,
        coder as coder_module,
        packager as packager_module,
        tester as tester_module,
    )

    async def noop(self, ctx):
        ctx.setdefault("plan", {"layers": []})
        ctx.setdefault("artifacts", {})
        ctx.setdefault("artifacts_by_layer", {})
        return ctx

    monkeypatch.setattr(architect_module.ArchitectStage, "run", noop)
    monkeypatch.setattr(coder_module.CoderStage, "run", noop)
    monkeypatch.setattr(tester_module.TesterStage, "run", noop)
    monkeypatch.setattr(packager_module.PackagerStage, "run", noop)

    req = BuildRequest(goal="x", reviewer="round-robin")
    await orch.build(req)

    assert RoundRobinReviewer in seen
    assert ReviewerStage not in seen

    # Restore originals (monkeypatch handles teardown but defensive cleanup
    # in case the test framework leaks state).
    _ = original_run, original_reviewer_run


@pytest.mark.asyncio
async def test_orchestrator_default_uses_reviewer_stage(tmp_board, monkeypatch):
    """``BuildRequest()`` (default reviewer) → ReviewerStage in pipeline."""
    fake = FakeLMClient(responses=[])
    orch = Orchestrator(fake, tmp_board)

    seen: list[type] = []

    async def spy_rr_run(self, ctx):
        seen.append(RoundRobinReviewer)
        ctx["review"] = {}
        return ctx

    async def spy_rs_run(self, ctx):
        seen.append(ReviewerStage)
        ctx["review"] = {}
        return ctx

    monkeypatch.setattr(RoundRobinReviewer, "run", spy_rr_run)
    monkeypatch.setattr(ReviewerStage, "run", spy_rs_run)

    from development.stages import (
        architect as architect_module,
        coder as coder_module,
        packager as packager_module,
        tester as tester_module,
    )

    async def noop(self, ctx):
        ctx.setdefault("plan", {"layers": []})
        ctx.setdefault("artifacts", {})
        ctx.setdefault("artifacts_by_layer", {})
        return ctx

    monkeypatch.setattr(architect_module.ArchitectStage, "run", noop)
    monkeypatch.setattr(coder_module.CoderStage, "run", noop)
    monkeypatch.setattr(tester_module.TesterStage, "run", noop)
    monkeypatch.setattr(packager_module.PackagerStage, "run", noop)

    req = BuildRequest(goal="x")  # default reviewer="single-pass"
    await orch.build(req)

    assert ReviewerStage in seen
    assert RoundRobinReviewer not in seen


@pytest.mark.asyncio
async def test_orchestrator_does_not_mutate_self_stages(tmp_board):
    """After a round-robin build, self._stages still contains ReviewerStage."""
    fake = FakeLMClient(
        responses=[
            json.dumps(
                {
                    "stack": {},
                    "layers": [],
                    "dependencies": [],
                }
            )
        ]
    )
    orch = Orchestrator(fake, tmp_board)
    pre_classes = [type(s) for s in orch.stages]

    # Run a build that would substitute the reviewer; we don't care about
    # the result, only that self._stages is preserved afterward.
    req = BuildRequest(goal="x", reviewer="round-robin")
    try:
        await orch.build(req)
    except Exception:  # noqa: BLE001 — irrelevant; test is about the stages list
        pass

    post_classes = [type(s) for s in orch.stages]
    assert pre_classes == post_classes
    # Sanity: ReviewerStage (not RoundRobinReviewer) is still in the
    # default pipeline shape.
    assert ReviewerStage in post_classes
    assert RoundRobinReviewer not in post_classes
