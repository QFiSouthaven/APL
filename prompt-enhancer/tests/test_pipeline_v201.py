"""v2.0.1 pipeline wirings — pipeline_graph + mcp hooks + model_router.

These tests cover the OPTIONAL parameters added in v2.0.1. Defaults
(all None) preserve every pre-v2.0.1 behavior — the existing
``test_concurrency.py`` regression guards continue to assert that.
"""

from __future__ import annotations

from typing import Any

import pytest

from enhancer.core.events import EventType
from enhancer.core.pipeline import PipelineOptions, run_pipeline
from enhancer.core.pipeline_graph import (
    PassNode,
    PipelineGraph,
    PipelineGraphValidationError,
    default_graph,
)


# Canned responses so the pipeline reaches Pass 4 without LLM weirdness.
PASS1_TOKENS = [
    "GOAL: Build a feature.\n",
    "DOMAIN: Software.\n",
    "TASK TYPE: analytical\n",
    "AUDIENCE: Developers.\n",
    "IMPLICIT NEEDS: clarity.\n",
]
PASS2_TOKENS = [
    "VAGUE TERMS: none\n",
    "MISSING CONTEXT: none\n",
    "UNSTATED CONSTRAINTS: none\n",
    "SCOPE ISSUES: none\n",
    "PRIMARY FOCUS: precision\n",
]
PASS3_TOKENS = ["Rewritten ", "prompt."]
PASS4_TOKENS = [
    "SPECIFICITY: 9\nCONSTRAINTS: 8\nACTIONABILITY: 9\nIMPROVEMENT: 70\n",
]


def _seed_minimal(provider) -> None:
    provider.stream_responses.extend([
        PASS1_TOKENS, PASS2_TOKENS, PASS3_TOKENS, PASS4_TOKENS,
    ])


# ── pipeline_graph validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_graph_valid_emits_step_event(fake_provider, event_collector):
    """A valid graph passes validation and emits an AGENT_STEP event."""
    _seed_minimal(fake_provider)
    on_event, events = event_collector

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        pipeline_graph=default_graph(),
    )

    step_events = [
        kwargs for (name, kwargs) in events
        if name == EventType.AGENT_STEP.value
        and kwargs.get("step") == "pipeline_graph_loaded"
    ]
    assert len(step_events) == 1
    assert "validated" in step_events[0]["detail"]


@pytest.mark.asyncio
async def test_pipeline_graph_invalid_raises_before_any_call(fake_provider):
    """An invalid graph must raise BEFORE any LLM call — fast-fail guard."""
    # idle_timeout != 120 on a streaming pass = invariant 3 violation.
    bad = PipelineGraph(
        nodes=(
            PassNode(
                id="pass1", kind="intent_analysis",
                streams=True, idle_timeout=60,
            ),
        ),
        version=1,
    )

    with pytest.raises(PipelineGraphValidationError):
        await run_pipeline(
            "Build me a thing.",
            provider=fake_provider, model="fake-7b",
            opts=PipelineOptions(),
            request_timeout=10.0, idle_timeout=120.0,
            pipeline_graph=bad,
        )

    # Critical: NO chat_stream calls were made — the validator caught
    # the misconfig before the pipeline started.
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    assert streams == []


# ── MCP pre-pass hooks ──────────────────────────────────────────────


class _FakeInvoker:
    """Minimal MCPToolInvoker stand-in — records calls + returns canned data."""

    def __init__(self, results: dict[tuple[str, str], Any] | None = None) -> None:
        self.calls: list[dict] = []
        self._results = results or {}

    async def invoke_with_events(
        self, *, server: str, tool: str, args: dict, on_event=None,
    ) -> dict:
        self.calls.append({"server": server, "tool": tool, "args": dict(args)})
        return self._results.get((server, tool), {"content": f"{server}/{tool} OK"})


@pytest.mark.asyncio
async def test_mcp_pre_pass1_enriches_user_message(fake_provider, event_collector):
    """mcp_pre_pass1 hooks run BEFORE Pass 1 and their results land in the user msg."""
    _seed_minimal(fake_provider)
    on_event, _events = event_collector

    invoker = _FakeInvoker(results={
        ("docs", "search"): {"content": "found 3 relevant articles"},
    })
    hooks = [{"server": "docs", "tool": "search", "args": {"q": "topic"}}]

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        mcp_invoker=invoker, mcp_pre_pass1=hooks,
    )

    # Hook fired exactly once with the right args
    assert invoker.calls == [
        {"server": "docs", "tool": "search", "args": {"q": "topic"}},
    ]
    # Pass 1's user message contains the enrichment block
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    pass1_user = next(
        (m["content"] for m in streams[0].messages if m["role"] == "user"),
        "",
    )
    assert "[MCP CONTEXT]" in pass1_user
    assert "found 3 relevant articles" in pass1_user
    assert "[END MCP CONTEXT]" in pass1_user


@pytest.mark.asyncio
async def test_mcp_pre_pass3_enriches_rewrite(fake_provider, event_collector):
    """mcp_pre_pass3 hooks land in Pass 3's user message."""
    _seed_minimal(fake_provider)
    on_event, _events = event_collector

    invoker = _FakeInvoker(results={
        ("schema", "lookup"): {"content": "Field: name (string), required"},
    })
    hooks = [{"server": "schema", "tool": "lookup", "args": {}}]

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        mcp_invoker=invoker, mcp_pre_pass3=hooks,
    )

    assert len(invoker.calls) == 1
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    pass3_user = next(
        (m["content"] for m in streams[2].messages if m["role"] == "user"),
        "",
    )
    assert "[MCP CONTEXT]" in pass3_user
    assert "Field: name (string), required" in pass3_user


@pytest.mark.asyncio
async def test_mcp_hook_failure_does_not_break_pipeline(fake_provider, event_collector):
    """A failing MCP hook is logged + skipped; pipeline continues to completion."""
    _seed_minimal(fake_provider)
    on_event, _events = event_collector

    class _BoomInvoker:
        async def invoke_with_events(self, **kwargs):
            raise RuntimeError("boom")

    hooks = [{"server": "x", "tool": "y", "args": {}}]
    result = await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        mcp_invoker=_BoomInvoker(), mcp_pre_pass1=hooks,
    )
    # Pipeline finished (no exception); enriched output exists.
    assert result.result == "Rewritten prompt."
    # Pass 1's user message has NO MCP CONTEXT block (hook errored).
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    pass1_user = next(
        (m["content"] for m in streams[0].messages if m["role"] == "user"),
        "",
    )
    assert "[MCP CONTEXT]" not in pass1_user


@pytest.mark.asyncio
async def test_mcp_hooks_without_invoker_are_silently_ignored(fake_provider, event_collector):
    """mcp_pre_pass1 supplied but mcp_invoker is None → no enrichment."""
    _seed_minimal(fake_provider)
    on_event, _events = event_collector

    hooks = [{"server": "x", "tool": "y", "args": {}}]
    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
        mcp_invoker=None, mcp_pre_pass1=hooks,
    )
    streams = [c for c in fake_provider.calls if c.kind == "chat_stream"]
    pass1_user = next(
        (m["content"] for m in streams[0].messages if m["role"] == "user"),
        "",
    )
    assert "[MCP CONTEXT]" not in pass1_user


# ── model_router scorer auto-selection ──────────────────────────────


@pytest.mark.asyncio
async def test_model_router_picks_scorer_when_unset(fake_provider, event_collector):
    """When opts.scorer_model is empty, model_router picks based on task_type."""
    _seed_minimal(fake_provider)
    on_event, events = event_collector

    # FakeChatProvider's list_models returns whatever we set.
    fake_provider.available_models = [
        "qwen3-coder-instruct",  # router prefers this for analytical
        "llama-3-8b",
        "mistral-7b",
    ]

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),  # scorer_model unset -> router takes over
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
    )

    # The pipeline_summary event records scorer_model
    summary = next(
        kwargs for (name, kwargs) in events
        if name == EventType.AGENT_PIPELINE_SUMMARY.value
    )
    # Pass 1 returned task_type=analytical → router picks qwen3-coder
    assert summary["scorer_model"] == "qwen3-coder-instruct"


@pytest.mark.asyncio
async def test_explicit_scorer_model_skips_router(fake_provider, event_collector):
    """If opts.scorer_model is set explicitly, router is bypassed."""
    _seed_minimal(fake_provider)
    on_event, events = event_collector

    fake_provider.available_models = ["qwen3-coder-instruct", "llama-3-8b"]

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(scorer_model="explicit-scorer"),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
    )

    summary = next(
        kwargs for (name, kwargs) in events
        if name == EventType.AGENT_PIPELINE_SUMMARY.value
    )
    assert summary["scorer_model"] == "explicit-scorer"


@pytest.mark.asyncio
async def test_model_router_fails_safe_to_default_model(fake_provider, event_collector):
    """If list_models throws, scorer_model falls back to `model`."""
    _seed_minimal(fake_provider)
    on_event, events = event_collector

    async def _boom():
        raise RuntimeError("LM Studio down")

    fake_provider.list_models = _boom  # type: ignore[method-assign]

    await run_pipeline(
        "Build me a thing.",
        provider=fake_provider, model="fake-7b",
        opts=PipelineOptions(),
        on_event=on_event,
        request_timeout=10.0, idle_timeout=120.0,
    )

    summary = next(
        kwargs for (name, kwargs) in events
        if name == EventType.AGENT_PIPELINE_SUMMARY.value
    )
    # Fell back to `model` because list_models errored.
    assert summary["scorer_model"] == "fake-7b"
