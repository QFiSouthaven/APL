"""Tests for the v2.1 ReasoningPanel wiring across the development service.

Scope:

  * The ``development.reasoning_panel`` re-export module (path-injection
    mirror of ``development.llm_client``) actually exposes the canonical
    names from ``enhancer.llm.reasoning_panel``.
  * The ``Stage.__init__`` ABC accepts ``reasoning_panel=None`` cleanly,
    and every existing concrete stage subclass still constructs
    unchanged when no panel is supplied.
  * ``ReviewerStage`` with no panel produces the same verdict shape
    as v0.3 (regression guard).
  * ``ReviewerStage`` with a 3-slot panel produces verdicts AND
    populates ``ctx["review"][layer]["panel"]`` with per-slot data.
  * ``primary-wins`` aggregator returns the primary slot's verdict;
    partner verdicts surface in the panel field for observability.
  * ``consensus-vote`` aggregator folds parseable JSON verdicts
    across slots.
  * One panel slot crashing does not kill the Reviewer stage.
  * ``Orchestrator(reasoning_panel=...)`` actually threads the panel
    into the ReviewerStage instance (and only the ReviewerStage in
    v2.1).
  * ``BuildRequest.to_dict()`` includes the new ``panel_mode`` and
    ``panel_aggregator`` fields with their default values.
  * Bounded-loopback contract still holds when panel is wired:
    one Coder loopback per layer, then a single re-critique that also
    routes through the panel.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.reasoning_panel import (
    DEFAULT_AGGREGATOR,
    DEFAULT_MODE,
    VALID_AGGREGATORS,
    VALID_MODES,
    LLMSlot,
    PanelResult,
    ReasoningPanel,
    SlotResponse,
)
from development.stages import (
    ArchitectStage,
    CoderStage,
    PackagerStage,
    ReviewerStage,
    TesterStage,
)
from development.stages import reviewer as reviewer_module
from development.types import BuildRequest

from tests.conftest import FakeLMClient


# ── helpers ─────────────────────────────────────────────────────────


def _verdict_json(approved: bool, issues: list[str], request_regenerate: bool) -> str:
    return json.dumps(
        {
            "approved": approved,
            "issues": issues,
            "request_regenerate": request_regenerate,
        }
    )


class _FakePanelProvider:
    """Minimal ChatProvider stand-in for panel slots.

    Slots in a real panel call ``provider.chat(messages, model=..., ...)``
    via ``_call_slot``. This fake records every call and returns a
    canned response (one per call, last sticks) — same idiom as the
    panel test helper in prompt-enhancer's test suite.
    """

    def __init__(
        self,
        responses: list[str],
        *,
        raise_after_n: int | None = None,
    ) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.raise_after_n = raise_after_n

    async def chat(self, messages, *, model, **kwargs):
        self.calls.append({"messages": list(messages), "model": model, **kwargs})
        if self.raise_after_n is not None and len(self.calls) > self.raise_after_n:
            raise RuntimeError("fake provider crash")
        if not self.responses:
            return ""
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover — unused
        if False:
            yield ""

    async def list_models(self):  # pragma: no cover — unused
        return []


def _slot(
    name: str,
    *,
    responses: list[str],
    weight: float = 1.0,
    raise_after_n: int | None = None,
) -> LLMSlot:
    return LLMSlot(
        name=name,
        provider=_FakePanelProvider(responses, raise_after_n=raise_after_n),
        model="fake-model",
        role="",
        weight=weight,
    )


def _ctx(
    *,
    artifacts: dict[str, dict[str, str]] | None = None,
    plan_layers: list[dict[str, Any]] | None = None,
    request: BuildRequest | None = None,
    board: MessageBoard | None = None,
) -> dict[str, Any]:
    nested = dict(artifacts or {})
    return {
        "build_request": request or BuildRequest(goal="x"),
        "plan": {"layers": plan_layers or []},
        "artifacts": {},
        "artifacts_by_layer": nested,
        "message_board": board,
    }


# ── 1. re-export module ─────────────────────────────────────────────


def test_reasoning_panel_module_exports_canonical_names():
    """All eight names re-export cleanly from the sibling import."""
    assert ReasoningPanel is not None
    assert LLMSlot is not None
    assert PanelResult is not None
    assert SlotResponse is not None
    assert DEFAULT_MODE in VALID_MODES
    assert DEFAULT_AGGREGATOR in VALID_AGGREGATORS
    # The constants are the same objects as the canonical module's.
    from enhancer.llm.reasoning_panel import (  # type: ignore[import-not-found]
        ReasoningPanel as Canonical,
    )
    assert ReasoningPanel is Canonical


# ── 2. Stage.__init__ accepts the kwarg cleanly (regression guard) ──


def test_every_stage_accepts_reasoning_panel_none(fake_lm):
    """All v2.0 stage subclasses must construct with reasoning_panel=None."""
    for cls in (
        ArchitectStage,
        CoderStage,
        ReviewerStage,
        TesterStage,
        PackagerStage,
    ):
        # Should not raise. The Coder has additional kwargs; everyone
        # else just inherits Stage.__init__.
        instance = cls(fake_lm, reasoning_panel=None)
        assert instance._reasoning_panel is None


def test_stages_default_to_no_panel(fake_lm):
    """Omitting reasoning_panel is identical to passing None."""
    stage = ReviewerStage(fake_lm)
    assert stage._reasoning_panel is None


# ── 3. Reviewer w/o panel reproduces v0.3 verdict shape ─────────────


@pytest.mark.asyncio
async def test_reviewer_without_panel_unchanged(monkeypatch):
    """v0.3 single-LLM critique path is preserved when panel is None."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(responses=[_verdict_json(True, [], False)])
    stage = ReviewerStage(fake)  # no panel

    ctx = _ctx(
        artifacts={"backend": {"app.py": "print('hi')"}},
        plan_layers=[{"name": "backend", "purpose": "rest api"}],
    )
    out = await stage.run(ctx)

    # Verdict shape is unchanged.
    review = out["review"]["backend"]
    assert review["approved"] is True
    assert review["issues"] == []
    assert review["request_regenerate"] is False
    # No panel telemetry key when no panel is wired.
    assert "panel" not in review
    # The single FakeLMClient was used (panel path would have left it
    # untouched).
    assert len(fake.calls) == 1


# ── 4. Reviewer with a panel populates ctx["review"][layer]["panel"] ─


@pytest.mark.asyncio
async def test_reviewer_with_3slot_panel_records_per_slot_telemetry(monkeypatch):
    """3-slot panel: aggregated drives the verdict; per-slot raw verdicts
    surface in ctx["review"][layer]["panel"]."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    primary = _slot(
        "primary",
        responses=[_verdict_json(True, [], False)],
    )
    critic = _slot(
        "critic",
        responses=[_verdict_json(False, ["picky concern"], False)],
    )
    alt = _slot(
        "alt",
        responses=[_verdict_json(True, ["looks fine"], False)],
    )
    panel = ReasoningPanel([primary, critic, alt])

    fake = FakeLMClient(responses=[])  # should never be called
    stage = ReviewerStage(fake, reasoning_panel=panel)

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "rest api"}],
    )
    out = await stage.run(ctx)

    review = out["review"]["backend"]
    # Aggregator default is primary-wins → primary's verdict is canonical.
    assert review["approved"] is True
    assert review["issues"] == []
    # Panel telemetry dict has the documented shape.
    panel_field = review["panel"]
    assert panel_field["primary"] == _verdict_json(True, [], False)
    partner_names = [p["name"] for p in panel_field["partners"]]
    assert partner_names == ["critic", "alt"]
    # Each partner entry has content / ms / error keys.
    for p in panel_field["partners"]:
        assert "content" in p
        assert "ms" in p
        assert p["error"] is None
    # The bare LLMClient was bypassed entirely — only the panel was used.
    assert len(fake.calls) == 0


# ── 5. primary-wins surfaces partner verdicts but returns primary's ─


@pytest.mark.asyncio
async def test_primary_wins_returns_primary_partners_in_telemetry(monkeypatch):
    """primary-wins: aggregated text == primary; partners visible in panel field."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    primary = _slot(
        "primary",
        responses=[_verdict_json(True, [], False)],
    )
    critic = _slot(
        "critic",
        responses=[_verdict_json(False, ["x is broken"], True)],
    )
    panel = ReasoningPanel([primary, critic])

    stage = ReviewerStage(
        FakeLMClient(responses=[]), reasoning_panel=panel
    )

    request = BuildRequest(goal="x", panel_aggregator="primary-wins")
    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
        request=request,
    )
    out = await stage.run(ctx)

    review = out["review"]["backend"]
    # Canonical verdict is primary's (approved=True), NOT critic's.
    assert review["approved"] is True
    assert review["request_regenerate"] is False
    # Critic's view is preserved in telemetry for callers that want it.
    critic_entry = next(
        p for p in review["panel"]["partners"] if p["name"] == "critic"
    )
    assert "x is broken" in critic_entry["content"]


# ── 6. consensus-vote aggregator ────────────────────────────────────


@pytest.mark.asyncio
async def test_consensus_vote_folds_parseable_verdicts(monkeypatch):
    """Three slots emit JSON; consensus-vote folds majority per key."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    primary = _slot(
        "primary",
        responses=[_verdict_json(True, [], False)],
    )
    a = _slot(
        "a",
        responses=[_verdict_json(True, [], False)],
    )
    b = _slot(
        "b",
        # Lone dissent — minority loses.
        responses=[_verdict_json(False, ["only-i-disapprove"], True)],
    )
    panel = ReasoningPanel([primary, a, b])

    stage = ReviewerStage(
        FakeLMClient(responses=[]), reasoning_panel=panel
    )

    request = BuildRequest(goal="x", panel_aggregator="consensus-vote")
    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
        request=request,
    )
    out = await stage.run(ctx)

    review = out["review"]["backend"]
    # 2-of-3 said approved=True → consensus folds to True.
    assert review["approved"] is True
    assert review["request_regenerate"] is False


# ── 7. slot error tolerance ────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_slot_crashes_reviewer_still_produces_verdict(monkeypatch):
    """A panel slot raising mid-call → its SlotResponse has error set,
    other slots' outputs still feed the aggregator, Reviewer still
    yields a verdict."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    primary = _slot(
        "primary",
        responses=[_verdict_json(True, [], False)],
    )
    crashy = _slot(
        "crashy",
        responses=["unused"],
        raise_after_n=0,  # raises on first call
    )
    panel = ReasoningPanel([primary, crashy])

    stage = ReviewerStage(
        FakeLMClient(responses=[]), reasoning_panel=panel
    )

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
    )
    out = await stage.run(ctx)

    review = out["review"]["backend"]
    assert review["approved"] is True
    crashy_entry = next(
        p for p in review["panel"]["partners"] if p["name"] == "crashy"
    )
    assert crashy_entry["error"] is not None
    assert "crash" in crashy_entry["error"].lower()
    # Primary's content is intact.
    assert review["panel"]["primary"] == _verdict_json(True, [], False)


# ── 8. Orchestrator threads panel into the ReviewerStage ────────────


@pytest.mark.asyncio
async def test_orchestrator_threads_panel_into_reviewer(fake_lm, tmp_board):
    """Constructing the Orchestrator with reasoning_panel=... must wire
    that panel into the default-pipeline ReviewerStage (and only that
    stage in v2.1)."""
    primary = _slot("primary", responses=[_verdict_json(True, [], False)])
    panel = ReasoningPanel([primary])

    orch = Orchestrator(fake_lm, tmp_board, reasoning_panel=panel)

    # Pipeline shape unchanged (5 stages).
    stages = orch.stages
    assert [s.name for s in stages] == [
        "architect", "coder", "reviewer", "tester", "packager",
    ]
    # Reviewer got the panel.
    reviewer_stage = next(s for s in stages if s.name == "reviewer")
    assert reviewer_stage._reasoning_panel is panel
    # Other stages did NOT get the panel in v2.1 (they accept the kwarg
    # but the orchestrator only wires the Reviewer for now).
    for s in stages:
        if s.name == "reviewer":
            continue
        assert s._reasoning_panel is None


def test_orchestrator_default_panel_is_none(fake_lm, tmp_board):
    """Backward compat: omitting reasoning_panel leaves every stage's
    panel attr None (v2.0 behavior unchanged)."""
    orch = Orchestrator(fake_lm, tmp_board)
    for s in orch.stages:
        assert s._reasoning_panel is None


# ── 9. BuildRequest serializes the new fields ──────────────────────


def test_build_request_to_dict_includes_panel_fields():
    """BuildRequest.to_dict() now carries panel_mode + panel_aggregator."""
    req = BuildRequest(goal="hello")
    d = req.to_dict()
    assert d["panel_mode"] == "parallel"
    assert d["panel_aggregator"] == "primary-wins"
    # And explicit overrides round-trip.
    req2 = BuildRequest(
        goal="hello",
        panel_mode="sequential",
        panel_aggregator="consensus-vote",
    )
    d2 = req2.to_dict()
    assert d2["panel_mode"] == "sequential"
    assert d2["panel_aggregator"] == "consensus-vote"


# ── 10. Panel + bounded-loopback interact correctly ────────────────


@pytest.mark.asyncio
async def test_panel_loopback_one_coder_regen_then_repanel(monkeypatch):
    """Panel rejects → ONE Coder loopback → re-critique also goes
    through the panel. Loopback budget unchanged from v0.3."""

    regen_calls: list[dict[str, Any]] = []
    regenerated_files = {"app.py": "# regenerated"}

    async def fake_gen(plan, layer_obj, llm, *, feedback=None):
        regen_calls.append({"feedback": feedback})
        return regenerated_files

    monkeypatch.setattr(
        reviewer_module, "LAYER_GENERATORS", {"backend": fake_gen}
    )

    # Panel emits TWO verdicts in sequence: first reject+regen, then approve.
    primary = _slot(
        "primary",
        responses=[
            _verdict_json(False, ["initial bug"], True),
            _verdict_json(True, [], False),
        ],
    )
    critic = _slot(
        "critic",
        responses=[
            _verdict_json(False, ["concur"], True),
            _verdict_json(True, [], False),
        ],
    )
    panel = ReasoningPanel([primary, critic])

    stage = ReviewerStage(
        FakeLMClient(responses=[]), reasoning_panel=panel
    )

    ctx = _ctx(
        artifacts={"backend": {"app.py": "old"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
    )
    out = await stage.run(ctx)

    # Exactly ONE Coder regen happened (bounded loopback).
    assert len(regen_calls) == 1
    assert regen_calls[0]["feedback"] == ["initial bug"]
    # The regenerated artifacts replaced the old ones.
    assert out["artifacts_by_layer"]["backend"] == regenerated_files
    # Final verdict (after re-panel-call) is the post-regen accept.
    review = out["review"]["backend"]
    assert review["approved"] is True
    # Panel telemetry reflects the SECOND consultation (post-regen).
    assert review["panel"]["primary"] == _verdict_json(True, [], False)
    # Layer was tracked as regen'd (so further loopbacks would be refused).
    assert "backend" in out["_reviewer_loopbacks"]
    # Each panel slot was called exactly twice (initial + post-regen).
    assert len(primary.provider.calls) == 2
    assert len(critic.provider.calls) == 2


# ── 11. Panel field is absent when no panel is wired (regression) ──


@pytest.mark.asyncio
async def test_panel_field_absent_when_no_panel_wired(monkeypatch):
    """Callers can detect 'panel-aware build' by ``"panel" in review``;
    when no panel is wired, that key MUST be absent so the v0.3 contract
    is preserved exactly."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(responses=[_verdict_json(True, [], False)])
    stage = ReviewerStage(fake)  # no panel

    ctx = _ctx(
        artifacts={"backend": {"app.py": "x"}},
        plan_layers=[{"name": "backend", "purpose": "p"}],
    )
    out = await stage.run(ctx)

    # Hard regression guard.
    assert "panel" not in out["review"]["backend"]


# ── 12. _critique compat shim still returns just a verdict dict ────


@pytest.mark.asyncio
async def test_critique_helper_returns_verdict_dict_for_round_robin(monkeypatch):
    """RoundRobinReviewer's deferred-fallback path calls ReviewerStage._critique
    directly and expects a verdict dict. The compat shim must preserve that."""
    monkeypatch.setattr(reviewer_module, "LAYER_GENERATORS", {})

    fake = FakeLMClient(responses=[_verdict_json(True, [], False)])
    stage = ReviewerStage(fake)

    verdict = await stage._critique(
        "backend",
        {"name": "backend", "purpose": "p"},
        {"app.py": "x"},
    )
    # Plain dict, not a (verdict, telemetry) tuple — old callers depend
    # on this shape.
    assert isinstance(verdict, dict)
    assert verdict["approved"] is True
    assert verdict["issues"] == []
