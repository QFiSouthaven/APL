"""End-to-end ReasoningPanel integration test against a real LM Studio.

Marked ``slow`` (the project's existing marker for tests that hit a real
LM backend -- registered in ``pyproject.toml``). Auto-skipped when
LM Studio is unreachable or fewer than 2 chat-capable models are loaded.

Run explicitly::

    pytest -m slow tests/test_integration_panel_lmstudio.py
    pytest -m "not slow"          # CI default -- skips this file

The skip predicate probes ``/api/v0/models`` (LM Studio's mgmt endpoint)
synchronously at module-import time, mirroring the prior art at
``development/tests/test_integration_lmstudio.py``. If two or more models
report ``state == "loaded"`` and ``type`` is chat-capable (``llm`` or
``vlm``), the tests run; otherwise they skip with an actionable message.
"""

from __future__ import annotations

import httpx
import pytest

from enhancer.core.events import EventType
from enhancer.core.pipeline import (
    PipelineOptions,
    build_resume_state,
    run_pipeline,
)
from enhancer.llm.lmstudio import LMStudioProvider
from enhancer.llm.reasoning_panel import LLMSlot, ReasoningPanel


LMS_MGMT_URL = "http://127.0.0.1:1234"
CHAT_TYPES = {"llm", "vlm"}
PROMPT = (
    "Write a concise commit message for adding rate limiting to a "
    "REST endpoint."
)


def _loaded_chat_models() -> list[str]:
    """Probe LM Studio at module-import time; return loaded chat-capable IDs."""
    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.get(f"{LMS_MGMT_URL}/api/v0/models")
            r.raise_for_status()
            data = r.json().get("data", [])
    except (httpx.HTTPError, ValueError):
        return []

    out: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("state") != "loaded":
            continue
        if entry.get("type") not in CHAT_TYPES:
            continue
        mid = entry.get("id")
        if mid:
            out.append(str(mid))
    return out


# Sorted so order is deterministic across the suite. ``hermes-3-llama-3.1-8b``
# < ``nsfwvision-...`` is the real-world example that motivated this -- the
# LM Studio API returns models in load-order, not alphabetic, which
# previously made which-model-is-primary depend on which test ran first.
_LOADED = sorted(_loaded_chat_models())
_SKIP_REASON = (
    "LM Studio integration test requires >=2 chat models loaded at "
    f"{LMS_MGMT_URL}; found {len(_LOADED)} ({_LOADED}). "
    "Load with `lms load <model>` x2 (or via the LM Studio desktop UI)."
)


async def _run_with_skip_clarify(
    *,
    panel: ReasoningPanel,
    primary_model: str,
):
    """Run ``run_pipeline`` and auto-resume past any disambiguation pause.

    Mirrors ``cli.main --skip-clarify`` semantics: if Pass 2 produced
    enough weakness fields to trigger interactive clarification, capture
    the disambig event, build an empty resume_state, and re-invoke. This
    keeps the test deterministic across primary models that vary in how
    aggressively they flag weaknesses.

    Returns the FINAL ``PipelineResult`` (post-resume if applicable) plus
    the merged panel telemetry: pre-resume captures Pass 1/2 telemetry
    that the empty-result sentinel would otherwise drop.
    """
    provider = panel.primary.provider
    pending: dict[str, dict] = {}
    captured: dict = {}
    pre_resume_panel: dict = {}

    async def on_event(et, **kw):
        # Capture disambig metadata so we can resume.
        if et == EventType.AGENT_DISAMBIGUATE:
            captured["disambig_id"] = kw.get("disambig_id")
            captured["questions"] = kw.get("questions") or []

    result = await run_pipeline(
        PROMPT,
        provider=provider,
        model=primary_model,
        opts=PipelineOptions(scorer_model=primary_model),
        reasoning_panel=panel,
        panel_mode="parallel",
        panel_aggregator="primary-wins",
        request_timeout=300.0,
        idle_timeout=120.0,
        on_event=on_event,
        pending_disambig=pending,
    )

    # If the pipeline paused, resume with no answers (skip-clarify).
    if (
        result.extras
        and result.extras.get("paused")
        and captured.get("disambig_id")
    ):
        snapshot = pending[captured["disambig_id"]]
        # We need to re-run pass1/2/4 telemetry through the panel; the
        # cleanest path is to re-invoke with a fresh prompt rather than
        # resume_state (which would skip Pass 1/2 entirely and bypass the
        # panel for those passes). Resume_state injects clarifications
        # into Pass 3 only, so the panel still wires Pass 4 normally.
        result = await run_pipeline(
            snapshot["prompt"],
            provider=provider,
            model=primary_model,
            opts=PipelineOptions(
                scorer_model=primary_model,
                resume_state=build_resume_state(snapshot, {}),
            ),
            reasoning_panel=panel,
            panel_mode="parallel",
            panel_aggregator="primary-wins",
            request_timeout=300.0,
            idle_timeout=120.0,
        )
        # Resume path skips Pass 1/2 entirely -- they're carried verbatim
        # from the parent. The panel telemetry for those passes is gone.
        # The test contract documents this: when a pause happens, only
        # pass3 and pass4 telemetry are guaranteed present.
        pre_resume_panel = {"resumed": True}

    return result, pre_resume_panel


def _build_panel(
    models: list[str],
    *,
    bad_partner_model: str | None = None,
) -> ReasoningPanel:
    """Build a primary + 1-partner panel.

    All slots share one ``LMStudioProvider`` instance (same host) -- the
    differentiation between slots is the model id passed at the
    ``/chat/completions`` layer.

    When ``bad_partner_model`` is supplied, slot 1 uses that id (which
    LM Studio rejects). The partner failure must be isolated to the
    partner's telemetry; the primary's content stays untouched.
    """
    provider = LMStudioProvider(
        base_url=f"{LMS_MGMT_URL}/v1", management_url=LMS_MGMT_URL,
    )
    slots = [LLMSlot(name="primary", provider=provider, model=models[0])]
    partner_model = bad_partner_model or models[1]
    slots.append(
        LLMSlot(
            name="partner_1",
            provider=provider,
            model=partner_model,
            role="alternative perspective",
        )
    )
    return ReasoningPanel(slots)


@pytest.mark.slow
@pytest.mark.skipif(len(_LOADED) < 2, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_panel_pass1_returns_real_partner_content():
    """Pass 1 (or Pass 3 post-resume) panel telemetry has the expected shape.

    When Pass 2 detects >=3 weakness fields, the pipeline pauses for
    disambiguation and -- on the auto-resume path -- skips Pass 1/2
    telemetry. We assert the FIRST pass that survives both paths
    (Pass 3) has a non-empty primary AND a partner with content. This
    proves the live panel produced real partner output even when the
    weakness-driven pause kicks in.
    """
    panel = _build_panel(_LOADED)
    result, info = await _run_with_skip_clarify(
        panel=panel, primary_model=_LOADED[0],
    )

    panel_tel = (result.extras or {}).get("panel")
    assert panel_tel is not None, (
        f"panel telemetry missing; extras={result.extras!r}"
    )

    # Pass 3 telemetry is present in BOTH paths (pre-pause and post-resume).
    assert "pass3" in panel_tel, f"pass3 missing; got keys={list(panel_tel)}"
    pass3 = panel_tel["pass3"]
    assert pass3["primary"], "primary returned empty content for pass3"
    partners = pass3["partners"]
    assert len(partners) == 1, f"expected 1 partner, got {len(partners)}"
    p = partners[0]
    assert {"name", "content", "ms", "error"} <= set(p.keys())
    assert p["error"] is None, f"partner errored: {p['error']}"
    assert p["content"], "partner returned empty content"
    assert p["ms"] >= 0

    # If we did NOT pause, Pass 1 telemetry should also be present.
    if not info.get("resumed"):
        assert "pass1" in panel_tel
        pass1 = panel_tel["pass1"]
        assert pass1["primary"], "pass1 primary empty (no-pause path)"
        assert len(pass1["partners"]) == 1
        assert pass1["partners"][0]["error"] is None


@pytest.mark.slow
@pytest.mark.skipif(len(_LOADED) < 2, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_panel_pass3_streams_primary_and_records_partners():
    """Pass 3: primary streams real content; partner runs concurrently."""
    panel = _build_panel(_LOADED)
    result, _info = await _run_with_skip_clarify(
        panel=panel, primary_model=_LOADED[0],
    )

    panel_tel = (result.extras or {}).get("panel") or {}
    assert "pass3" in panel_tel, f"pass3 missing; got keys={list(panel_tel)}"

    pass3 = panel_tel["pass3"]
    assert pass3["primary"], "pass3 primary stream returned empty"
    partners = pass3["partners"]
    assert len(partners) == 1
    p = partners[0]
    # Partner should have produced something; if it errored, surface why.
    if p["error"] is None:
        assert p["content"], "partner Pass 3 succeeded but content empty"
    else:
        pytest.fail(f"partner Pass 3 errored unexpectedly: {p['error']}")

    # The streamed primary equals the user-visible enhanced prompt
    # (modulo self-correction retry, which leaves panel_tel unchanged).
    assert result.result, "result.result empty despite non-empty pass3 primary"


@pytest.mark.slow
@pytest.mark.skipif(len(_LOADED) < 2, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_panel_partner_failure_isolated():
    """A bad partner-model id surfaces as a slot error; primary is unaffected."""
    panel = _build_panel(
        _LOADED,
        bad_partner_model="this-model-definitely-does-not-exist-xyz",
    )
    result, info = await _run_with_skip_clarify(
        panel=panel, primary_model=_LOADED[0],
    )

    assert result.result, "primary stream produced no enhanced prompt"

    panel_tel = (result.extras or {}).get("panel") or {}
    # Whichever pass survived (pass1 if no pause, pass3 if resumed),
    # the partner there must report an error and empty content while the
    # primary content is non-empty.
    candidate_keys = ["pass1", "pass3"] if not info.get("resumed") else ["pass3"]
    found = False
    for key in candidate_keys:
        entry = panel_tel.get(key)
        if entry is None:
            continue
        assert entry["primary"], f"{key} primary empty despite partner failure"
        assert len(entry["partners"]) == 1
        p = entry["partners"][0]
        if p["error"] is not None:
            assert p["content"] == ""
            found = True
            break
    assert found, (
        f"expected at least one pass to record the partner error; "
        f"telemetry={panel_tel!r}"
    )
