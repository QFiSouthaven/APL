"""Branching regression guards — fork from any completed pass.

These tests assert that ``run_pipeline`` can fork off a parent run at
Pass 1, 2, or 3 (never Pass 4 — that always re-runs against the new
prompt). The frozen ``EventType`` enum means we re-use ``AGENT_STEP``
with ``step="branch_start"`` to mark the branch — no new event types.

1. ``test_branch_from_pass_2_reuses_parent_passes`` — branching from
   Pass 2 with a new prompt: parent's pass1/pass2 outputs are reused
   verbatim and the FakeChatProvider is NOT called for those passes.
2. ``test_branch_persists_parent_link`` — child run row carries
   ``parent_run_id`` + ``parent_pass``.
3. ``test_branch_from_missing_parent_raises_clean_error`` — passing a
   nonexistent ``parent_run_id`` raises :class:`BranchError` and
   inserts no orphan row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from enhancer.core.events import EventType
from enhancer.core.pipeline import (
    BranchError,
    PipelineOptions,
    run_pipeline,
)
from enhancer.persistence import runs as runs_module


PARENT_PROMPT = "Parent prompt: do a thing."

PASS1_TOKENS = [
    "GOAL: original\n",
    "DOMAIN: testing\n",
    "TASK TYPE: analytical\n",
    "AUDIENCE: devs\n",
    "IMPLICIT NEEDS: clarity\n",
]
PASS2_TOKENS = [
    "VAGUE TERMS: none\n",
    "MISSING CONTEXT: none\n",
    "UNSTATED CONSTRAINTS: none\n",
    "SCOPE ISSUES: none\n",
    "PRIMARY FOCUS: precision\n",
]
PASS3_TOKENS = ["Parent ", "rewrite ", "result."]
PASS4_TOKENS = [
    "SPECIFICITY: 8\nCONSTRAINTS: 7\nACTIONABILITY: 9\nIMPROVEMENT: 60\n",
]

CHILD_PASS3_TOKENS = ["Branched ", "rewrite ", "for ", "child."]
CHILD_PASS4_TOKENS = [
    "SPECIFICITY: 9\nCONSTRAINTS: 8\nACTIONABILITY: 9\nIMPROVEMENT: 70\n",
]


def _seed_full_parent_run(provider) -> None:
    """Seed the four streaming responses for a complete parent run."""
    provider.stream_responses.extend(
        [PASS1_TOKENS, PASS2_TOKENS, PASS3_TOKENS, PASS4_TOKENS]
    )


@pytest.fixture
def db_tmp(tmp_path: Path) -> Path:
    """Tmp SQLite path for branch tests."""
    return tmp_path / "branching.db"


@pytest.mark.asyncio
async def test_branch_from_pass_2_reuses_parent_passes(
    fake_provider, event_collector, db_tmp
):
    """Branching from Pass 2 must reuse parent's pass1/pass2 verbatim
    and never invoke the provider for them."""
    on_event, events = event_collector

    # ── 1. run + persist the parent ─────────────────────────────────
    _seed_full_parent_run(fake_provider)
    parent_result = await run_pipeline(
        PARENT_PROMPT,
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=5.0,
    )
    parent_record = parent_result.extras["_record"]
    runs_module.save(parent_record, db_tmp)
    parent_id = parent_record.id

    # Snapshot what the parent put on the wire.
    parent_calls = list(fake_provider.calls)

    # ── 2. seed only Pass 3 + Pass 4 streams for the child ──────────
    fake_provider.stream_responses.extend([CHILD_PASS3_TOKENS, CHILD_PASS4_TOKENS])

    new_prompt = "Child prompt: do a different thing."
    child_result = await run_pipeline(
        new_prompt,
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(
            parent_run_id=parent_id,
            branch_from_pass=2,
            branch_db_path=db_tmp,
        ),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=5.0,
    )

    child_record = child_result.extras["_record"]
    runs_module.save(child_record, db_tmp)

    # ── 3. assertions ──────────────────────────────────────────────
    # The child ran exactly two new chat_streams: Pass 3 and Pass 4.
    new_streams = [
        c for c in fake_provider.calls[len(parent_calls):] if c.kind == "chat_stream"
    ]
    assert len(new_streams) == 2, (
        f"Branch should have run only Pass 3 + Pass 4 (got {len(new_streams)} "
        "streams). Pass 1 + Pass 2 must be reused from parent."
    )
    # No PASS1_SYSTEM nor PASS2_SYSTEM in the child's call set. Each
    # pass system prompt has a unique marker line that tells them apart.
    for c in new_streams:
        sys_text = next((m["content"] for m in c.messages
                         if m.get("role") == "system"), "")
        assert "GOAL: <what the user ultimately wants>" not in sys_text, (
            "Pass 1 system prompt re-sent during a branch from Pass 2"
        )
        assert "VAGUE TERMS:" not in sys_text, (
            "Pass 2 system prompt re-sent during a branch from Pass 2"
        )

    # Child's pass1/pass2 outputs match the parent verbatim.
    assert child_record.pass1_output == parent_record.pass1_output
    assert child_record.pass2_output == parent_record.pass2_output
    # Pass 3 differs — branched run produced a new enhancement.
    assert child_record.enhanced_prompt == "Branched rewrite for child."
    assert child_record.enhanced_prompt != parent_record.enhanced_prompt

    # AGENT_STEP fired with branch_start + parent_run_id + parent_pass.
    branch_starts = [
        p for n, p in events
        if n == EventType.AGENT_STEP.value and p.get("step") == "branch_start"
    ]
    assert len(branch_starts) == 1
    assert branch_starts[0]["parent_run_id"] == parent_id
    assert branch_starts[0]["parent_pass"] == 2


@pytest.mark.asyncio
async def test_branch_persists_parent_link(fake_provider, event_collector, db_tmp):
    """Child run row in SQLite has parent_run_id + parent_pass populated."""
    on_event, _ = event_collector

    _seed_full_parent_run(fake_provider)
    parent_result = await run_pipeline(
        PARENT_PROMPT,
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=5.0,
    )
    parent_record = parent_result.extras["_record"]
    runs_module.save(parent_record, db_tmp)
    parent_id = parent_record.id

    fake_provider.stream_responses.extend([CHILD_PASS3_TOKENS, CHILD_PASS4_TOKENS])
    child_result = await run_pipeline(
        "Child prompt.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(
            parent_run_id=parent_id,
            branch_from_pass=2,
            branch_db_path=db_tmp,
        ),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=5.0,
    )
    child_record = child_result.extras["_record"]
    runs_module.save(child_record, db_tmp)

    row = runs_module.get_run(db_tmp, child_record.id)
    assert row is not None
    assert row["parent_run_id"] == parent_id
    assert row["parent_pass"] == 2


@pytest.mark.asyncio
async def test_branch_from_missing_parent_raises_clean_error(
    fake_provider, event_collector, db_tmp
):
    """A nonexistent parent_run_id must raise BranchError, not a SQLite
    IntegrityError, and must NOT insert an orphan child row."""
    on_event, _ = event_collector

    # Initialize the database so the schema exists but has no runs.
    from enhancer.persistence.db import init_db
    init_db(db_tmp)

    # No streams seeded — if the pipeline gets past the branch check it
    # would crash on a different error and the test would mis-diagnose.
    with pytest.raises(BranchError) as exc_info:
        await run_pipeline(
            "Child prompt with bogus parent.",
            provider=fake_provider, model="fake-7b",
            opts=PipelineOptions(
                parent_run_id="nonexistent",
                branch_from_pass=2,
                branch_db_path=db_tmp,
            ),
            on_event=on_event,
            request_timeout=10.0, idle_timeout=5.0,
        )

    assert "nonexistent" in str(exc_info.value)

    # No orphan child row should have been inserted.
    from enhancer.persistence.db import connect
    with connect(db_tmp) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    assert count == 0, "BranchError should leave the runs table untouched"
