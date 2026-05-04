"""Stage-level tests for the Reviewer.

The Reviewer's responsibilities:

  1. Critique each (layer, files) entry in ``ctx["artifacts"]``.
  2. Record the verdict in ``ctx["review"][layer_name]``.
  3. On a ``request_regenerate`` verdict, invoke the matching layer
     generator from ``development.layers.LAYER_GENERATORS`` ONCE per
     layer per build, then re-critique. A second consecutive rejection
     is logged + accepted (bounded loopback per
     ``docs/DEVELOPMENT_FRAMEWORK.md`` §5).
  4. Publish ``STAGE_PROGRESS`` events with the per-layer summary.

Tests use the ``FakeLMClient`` from ``tests/conftest.py`` and monkeypatch
``LAYER_GENERATORS`` so we don't depend on the Coder agent's
implementation details landing first.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.stages import reviewer as reviewer_module
from development.stages.reviewer import (
    RETRY_REMINDER,
    SYSTEM_PROMPT,
    ReviewerStage,
)
from development.types import STAGE_PROGRESS, BuildRequest, LayerGenerationError

from tests.conftest import FakeLMClient


def _verdict(approved: bool, issues: list[str], request_regenerate: bool) -> str:
    """JSON-encode a Reviewer verdict for a FakeLMClient response slot."""
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
    """Build a minimal Reviewer-input ctx.

    The Reviewer reads ``ctx["artifacts_by_layer"]`` (nested per-layer view
    populated by the Coder); the flat ``ctx["artifacts"]`` view is
    rebuilt by the Reviewer at the end of run() from the nested view, so
    we don't pre-populate it here.
    """
    nested = dict(artifacts or {})
    return {
        "build_request": BuildRequest(goal="x"),
        "plan": {"layers": plan_layers or []},
        "artifacts": {},
        "artifacts_by_layer": nested,
        "message_board": board,
    }


# ── happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approved_verdict_records_review_and_skips_regen(monkeypatch):
    """A clean approval populates ctx["review"] with no loopback."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(responses=[_verdict(True, [], False)])
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "print('hi')"}},
        plan_layers=[{"name": "backend", "purpose": "rest api"}],
    )
    out = await stage.run(ctx)

    assert "backend" in out["review"]
    assert out["review"]["backend"]["approved"] is True
    assert out["review"]["backend"]["issues"] == []
    # Exactly one LLM call — no retry, no second critique.
    assert len(fake.calls) == 1
    # System prompt sent verbatim.
    assert fake.calls[0]["messages"][0]["content"] == SYSTEM_PROMPT
    # No regen tracking (no loopback happened).
    assert out["_reviewer_loopbacks"] == set()


# ── loopback paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_regenerate_triggers_one_loopback(monkeypatch):
    """Reject + request_regenerate=True calls the generator and re-critiques."""
    regenerated = {"app.py": "# v2 — improved\nprint('hi')"}
    gen_calls: list[dict[str, Any]] = []

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        gen_calls.append({"plan": plan, "layer": layer_obj, "feedback": feedback})
        return regenerated

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": fake_gen}
    )

    fake = FakeLMClient(
        responses=[
            _verdict(False, ["bug: missing return"], True),  # initial reject
            _verdict(True, [], False),                        # post-regen accept
        ]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "print('old')"}},
        plan_layers=[{"name": "backend", "purpose": "rest api"}],
    )
    out = await stage.run(ctx)

    assert len(gen_calls) == 1
    assert gen_calls[0]["feedback"] == ["bug: missing return"]
    # Regenerated artifacts replaced the old ones.
    assert out["artifacts_by_layer"]["backend"] == regenerated
    # Final verdict recorded is the post-regen one.
    assert out["review"]["backend"]["approved"] is True
    # Layer was tracked as having been regenerated once.
    assert "backend" in out["_reviewer_loopbacks"]
    # Two LLM calls: initial critique + post-regen critique.
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_rejected_without_request_regenerate_skips_loopback(monkeypatch):
    """approved=False but request_regenerate=False just records issues."""
    gen_called = False

    async def fake_gen(*args, **kwargs):
        nonlocal gen_called
        gen_called = True
        return {}

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": fake_gen}
    )

    fake = FakeLMClient(
        responses=[_verdict(False, ["architectural concern"], False)]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    assert gen_called is False
    assert out["review"]["backend"]["approved"] is False
    assert out["review"]["backend"]["issues"] == ["architectural concern"]
    assert out["_reviewer_loopbacks"] == set()
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_bounded_loopback_accepts_second_rejection(monkeypatch, caplog):
    """After one regen, a second rejection is accepted as-is — no infinite loop."""

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        return {"app.py": "# regen attempt"}

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": fake_gen}
    )

    fake = FakeLMClient(
        responses=[
            _verdict(False, ["bug A"], True),  # initial: reject + regen
            _verdict(False, ["bug B"], True),  # post-regen: still reject + regen
        ]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )

    with caplog.at_level(logging.WARNING, logger="development.stages.reviewer"):
        out = await stage.run(ctx)

    # Exactly two critiques — no third call. Bounded.
    assert len(fake.calls) == 2
    # Final verdict is the second (still-rejected) one.
    assert out["review"]["backend"]["approved"] is False
    assert out["review"]["backend"]["issues"] == ["bug B"]
    # Warning logged for the still-rejected-after-regen case.
    assert any(
        "still rejected" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_consecutive_rejections_log_warning(monkeypatch, caplog):
    """The bounded-loopback warning fires with the layer name + issues."""

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        return {"x.py": "still bad"}

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"frontend": fake_gen}
    )

    fake = FakeLMClient(
        responses=[
            _verdict(False, ["first issue"], True),
            _verdict(False, ["second issue"], True),
        ]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"frontend": {"x.py": "y"}},
        plan_layers=[{"name": "frontend", "purpose": "ui"}],
    )

    with caplog.at_level(logging.WARNING, logger="development.stages.reviewer"):
        await stage.run(ctx)

    matching = [r for r in caplog.records if "still rejected" in r.message]
    assert len(matching) == 1
    assert "frontend" in matching[0].message


# ── error handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_json_falls_back_to_approved(monkeypatch):
    """Two unparseable responses → fallback verdict, build continues."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(responses=["totally not json", "still nonsense"])
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    # Two calls — initial + retry.
    assert len(fake.calls) == 2
    # Retry uses the strict reminder.
    assert fake.calls[1]["messages"][-1]["content"] == RETRY_REMINDER
    # Fallback verdict treats it as approved (best-effort QC).
    assert out["review"]["backend"]["approved"] is True
    assert out["review"]["backend"]["issues"] == []
    assert out["review"]["backend"]["request_regenerate"] is False


@pytest.mark.asyncio
async def test_layer_with_no_matching_generator_skips_loopback(monkeypatch):
    """request_regenerate=True but no entry in LAYER_GENERATORS → skip cleanly."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(
        responses=[_verdict(False, ["needs work"], True)]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    # No loopback happened.
    assert out["_reviewer_loopbacks"] == set()
    # Only one LLM call — no second critique.
    assert len(fake.calls) == 1
    # Verdict still recorded with the rejection.
    assert out["review"]["backend"]["approved"] is False
    assert out["review"]["backend"]["issues"] == ["needs work"]


@pytest.mark.asyncio
async def test_generator_without_feedback_kwarg_falls_back(monkeypatch):
    """Old generator signature (no `feedback` kwarg) → call without it."""
    call_log: list[tuple[tuple, dict]] = []

    async def old_gen(plan, layer_obj, llm):  # no `feedback` kwarg
        call_log.append((("plan", "layer", "llm"), {}))
        return {"app.py": "# regenerated without feedback"}

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": old_gen}
    )

    fake = FakeLMClient(
        responses=[
            _verdict(False, ["fix this"], True),  # initial reject
            _verdict(True, [], False),             # post-regen accept
        ]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )
    out = await stage.run(ctx)

    # Generator called exactly once (the TypeError fallback path replaced
    # the failed kwargs call).
    assert len(call_log) == 1
    assert out["artifacts_by_layer"]["backend"]["app.py"] == "# regenerated without feedback"
    assert out["review"]["backend"]["approved"] is True
    assert "backend" in out["_reviewer_loopbacks"]


@pytest.mark.asyncio
async def test_generator_raises_LayerGenerationError(monkeypatch, caplog):
    """LayerGenerationError during loopback → caught, original artifacts kept."""
    original_files = {"app.py": "original content"}

    async def boom_gen(plan, layer_obj, llm, *, feedback=None):
        raise LayerGenerationError("backend", raw_response="garbage")

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": boom_gen}
    )

    fake = FakeLMClient(
        responses=[_verdict(False, ["fix me"], True)]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={"backend": dict(original_files)},
        plan_layers=[{"name": "backend", "purpose": "rest"}],
    )

    with caplog.at_level(logging.WARNING, logger="development.stages.reviewer"):
        out = await stage.run(ctx)

    # Pre-loopback artifacts preserved.
    assert out["artifacts_by_layer"]["backend"] == original_files
    # Layer was NOT marked as regenerated (the regen failed).
    assert "backend" not in out["_reviewer_loopbacks"]
    # Only the initial critique fired (no re-critique after failed regen).
    assert len(fake.calls) == 1
    # Warning logged.
    assert any(
        "LayerGenerationError" in rec.message
        for rec in caplog.records
    )


# ── ctx-shape + event-publishing tests ──────────────────────────────


@pytest.mark.asyncio
async def test_no_artifacts_returns_empty_review(monkeypatch, tmp_board):
    """Reviewer over an empty artifacts dict returns ctx["review"] = {}."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(responses=[])  # never called
    stage = ReviewerStage(fake)

    ctx = _ctx(artifacts={}, plan_layers=[], board=tmp_board)
    out = await stage.run(ctx)

    assert out["review"] == {}
    assert len(fake.calls) == 0
    # No STAGE_PROGRESS events should fire — there were no layers to review.
    progress = [e for e in tmp_board.recent(limit=20) if e.kind == STAGE_PROGRESS]
    assert progress == []


@pytest.mark.asyncio
async def test_multiple_layers_each_get_their_own_verdict(monkeypatch):
    """First layer's regen doesn't affect the second layer's flow."""

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        return {"new.py": "post-regen"}

    monkeypatch.setattr(
        reviewer_module,
        "LAYER_GENERATORS",
        {"backend": fake_gen, "frontend": fake_gen},
    )

    fake = FakeLMClient(
        responses=[
            _verdict(False, ["backend bug"], True),  # backend initial: reject + regen
            _verdict(True, [], False),               # backend post-regen: accept
            _verdict(True, [], False),               # frontend: clean accept
        ]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={
            "backend": {"app.py": "x"},
            "frontend": {"index.html": "<html/>"},
        },
        plan_layers=[
            {"name": "backend", "purpose": "rest"},
            {"name": "frontend", "purpose": "ui"},
        ],
    )
    out = await stage.run(ctx)

    assert out["review"]["backend"]["approved"] is True
    assert out["review"]["frontend"]["approved"] is True
    # Only backend was regenerated.
    assert out["_reviewer_loopbacks"] == {"backend"}
    # Three LLM calls: backend×2 (initial + post-regen) + frontend×1.
    assert len(fake.calls) == 3
    # Frontend artifacts untouched.
    assert out["artifacts_by_layer"]["frontend"] == {"index.html": "<html/>"}


@pytest.mark.asyncio
async def test_stage_progress_events_have_correct_shape(monkeypatch, tmp_board):
    """STAGE_PROGRESS payloads carry stage/layer/approved/issues_count/regenerated."""

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        return {"x.py": "regenerated"}

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": fake_gen}
    )

    fake = FakeLMClient(
        responses=[
            _verdict(False, ["a", "b"], True),  # backend: reject + regen
            _verdict(True, [], False),           # backend: post-regen accept
            _verdict(False, ["c"], False),       # frontend: reject, no regen
        ]
    )
    stage = ReviewerStage(fake)

    ctx = _ctx(
        artifacts={
            "backend": {"app.py": "x"},
            "frontend": {"i.html": "y"},
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
    # One event per layer.
    assert len(progress) == 2

    backend_evt = next(p for p in progress if p.payload["layer"] == "backend")
    assert backend_evt.payload["stage"] == "reviewer"
    assert backend_evt.payload["approved"] is True
    assert backend_evt.payload["issues_count"] == 0
    assert backend_evt.payload["regenerated"] is True

    frontend_evt = next(p for p in progress if p.payload["layer"] == "frontend")
    assert frontend_evt.payload["stage"] == "reviewer"
    assert frontend_evt.payload["approved"] is False
    assert frontend_evt.payload["issues_count"] == 1
    assert frontend_evt.payload["regenerated"] is False
