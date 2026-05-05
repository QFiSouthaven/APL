"""run_pipeline — the 4-pass prompt enhancer (transport-agnostic).

Lifted from ``swarm-agent-dev/src/webui/mods/agent_pipeline.py:874-1664``
(``_run_pipeline``). Every ``self.emit(ws, ...)`` is now ``await
on_event(EventType.X, **payload)`` — the only coupling point between
core and any UI.

**The three concurrency invariants are preserved verbatim. Read
``docs/EXTRACTION_GOTCHAS.md`` before touching this file.**

1. Pass 1 → Pass 2 are STRICTLY SERIAL. Never ``asyncio.gather``.
2. Pass 4 is awaited BEFORE Magnitude/SoT begin streaming.
3. Every ``provider.chat_stream`` call uses ``idle_timeout=120`` (the
   provider's default). Do not change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

from ..llm.base import ChatProvider
from ..persistence.runs import RunRecord
from .budgeting import (
    DEFAULT_CHAR_BUDGET,
    compute_pass_budgets,
    detect_context_budget,
    scaled,
    truncate,
)
from .events import EventType, P4_DEFAULTS, PipelineResult
from .parsing import (
    coerce_task_type_for_code,
    count_weakness_fields,
    parse_disambiguate_questions,
    parse_persona,
    parse_scores,
    parse_task_type,
    parse_technique,
)
from .passes import (
    DISAMBIGUATE_SYSTEM,
    PASS_NAMES,
    PASS1_SYSTEM,
    PASS2_SYSTEM,
    PASS4_SYSTEM,
    TECHNIQUE_GUIDANCE,
    select_pass3_system,
)
from .transforms import (
    MAGNITUDE_SYSTEM_PROMPT,
    PERSONA_FALLBACK,
    PERSONA_SYSTEM,
    SOT_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from .pipeline_graph import PipelineGraph
    from ..mcp.invoker import MCPToolInvoker
    from ..llm.reasoning_panel import ReasoningPanel

logger = logging.getLogger("enhancer.core.pipeline")

# Threshold for triggering interactive disambiguation.
DISAMBIGUATE_THRESHOLD = 3


# ── input options & resume state ──────────────────────────────────────

class BranchError(Exception):
    """Raised when a branch run cannot be initialized (missing parent, etc.)."""


@dataclass
class PipelineOptions:
    """User-controlled knobs for one pipeline run."""

    scorer_model: str | None = None
    magnitude_mode: bool = False
    persona_mode: bool = False
    sot_mode: bool = False
    session_id: str | None = None
    temperature: float = 0.7
    max_tokens_scale: float = 1.0
    # Internal — used by disambiguation resume; do not set from UI.
    resume_state: dict[str, Any] | None = None
    # Branching — set when forking from a previous run at a given pass.
    # ``branch_from_pass`` ∈ {1, 2, 3} indicates the last pass to copy
    # verbatim from the parent. ``parent_run_id`` is the parent's run id.
    # ``branch_db_path`` lets the pipeline load the parent record; when
    # ``None`` the default :func:`config.db_path` is used.
    branch_from_pass: int | None = None
    parent_run_id: str | None = None
    branch_db_path: Path | None = None


@dataclass
class _ResumeState:
    """Snapshot captured when the pipeline pauses for disambiguation."""

    pass1: str
    pass2: str
    task_type: str
    technique: str
    session_context: str
    t0: float
    t1: float
    disambiguation_context: str = ""


# ── event helpers ────────────────────────────────────────────────────

OnEvent = Callable[..., Awaitable[None]]


async def _emit(on_event: OnEvent | None, event: EventType, **kwargs: Any) -> None:
    """Best-effort event emit — never raises if the consumer disconnects."""
    if on_event is None:
        return
    try:
        await on_event(event, **kwargs)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("on_event %s suppressed: %s", event.value, exc)


# ── pretrial (model recommendation) ──────────────────────────────────

async def run_pretrial(
    prompt: str,
    *,
    provider: ChatProvider,
    model: str,
    on_event: OnEvent | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Single-shot model recommendation — emits ``PRETRIAL_*`` events."""
    from .transforms import PRETRIAL_SYSTEM
    await _emit(on_event, EventType.PRETRIAL_START)
    try:
        models = await provider.list_models()
    except Exception as exc:  # noqa: BLE001
        await _emit(on_event, EventType.PRETRIAL_ERROR, error=str(exc))
        return {"error": str(exc)}

    user_msg = (
        f"Prompt: {prompt}\n\nAvailable models:\n"
        + "\n".join(f"- {m}" for m in models)
    )
    try:
        text = await provider.chat(
            messages=[
                {"role": "system", "content": PRETRIAL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            model=model, timeout=timeout, temperature=0.4, max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001
        await _emit(on_event, EventType.PRETRIAL_ERROR, error=str(exc))
        return {"error": str(exc)}

    out: dict[str, Any] = {"raw": text}
    for line in text.splitlines():
        u = line.upper()
        if u.startswith("CATEGORY:"):
            out["category"] = line.split(":", 1)[1].strip()
        elif u.startswith("RECOMMENDED:"):
            out["recommended"] = line.split(":", 1)[1].strip()
        elif u.startswith("CONFIDENCE:"):
            out["confidence"] = line.split(":", 1)[1].strip()
        elif u.startswith("REASONING:"):
            out["reasoning"] = line.split(":", 1)[1].strip()
    out["available_models"] = models
    await _emit(on_event, EventType.PRETRIAL_RESULT, **out)
    return out


# ── core pipeline ───────────────────────────────────────────────────

async def run_pipeline(  # noqa: C901, PLR0912, PLR0915 — port of monolith _run_pipeline
    prompt: str,
    *,
    provider: ChatProvider,
    model: str,
    opts: PipelineOptions | None = None,
    on_event: OnEvent | None = None,
    session_context: str = "",
    request_timeout: float = 600.0,
    idle_timeout: float = 120.0,
    pending_disambig: dict[str, dict] | None = None,
    pipeline_graph: "PipelineGraph | None" = None,
    mcp_invoker: "MCPToolInvoker | None" = None,
    mcp_pre_pass1: list[dict] | None = None,
    mcp_pre_pass3: list[dict] | None = None,
    reasoning_panel: "ReasoningPanel | None" = None,
    panel_mode: str = "primary-only",
    panel_aggregator: str = "primary-wins",
) -> PipelineResult:
    """The 4-pass enhancer.

    Concurrency invariants — these are LOAD-BEARING:

    1. ``pass1 = await ...; pass2 = await ...`` — never gather.
    2. Pass 4 runs as a background task during Pass 3; it MUST be
       awaited before Magnitude / SoT begin.
    3. Every ``provider.chat_stream`` call carries ``idle_timeout``.

    v2.0.1 optional parameters (all default ``None`` and preserve every
    pre-v2.0.1 behavior when omitted):

    * ``pipeline_graph`` — if supplied, validated at call time. A graph
      that violates any of the three invariants raises
      :class:`PipelineGraphValidationError` BEFORE any LLM call. Once
      validated, the graph is informational in v2.0.1 (the pipeline still
      runs the canonical 4-pass order). True graph-driven scheduling is
      a v2.0.2+ refinement; we land the validation guard first.
    * ``mcp_invoker`` + ``mcp_pre_pass1`` / ``mcp_pre_pass3`` — when both
      the invoker and at least one hook list are supplied, the invoker
      runs the listed tool calls BEFORE Pass 1 (intent enrichment) and
      BEFORE Pass 3 (rewrite tools). Each hook is a dict with keys
      ``server``, ``tool``, ``args``. Tool results are concatenated and
      injected into the user message as ``[MCP CONTEXT]...[END MCP CONTEXT]``
      so Pass 1/3 can use the enrichment without protocol awareness.

    Returns a :class:`PipelineResult` dataclass. Caller is responsible
    for persisting it via ``persistence.runs.save(...)`` if desired.
    """
    opts = opts or PipelineOptions()
    pending_disambig = pending_disambig if pending_disambig is not None else {}
    scorer_model = opts.scorer_model or model
    temperature = opts.temperature
    max_tokens_scale = opts.max_tokens_scale

    # v2.1: panel telemetry — populated only when ``reasoning_panel`` is
    # supplied. Preserves byte-identical behavior when None: the panel
    # branches are skipped entirely and existing chat_stream paths run.
    panel_telemetry: dict[str, Any] = {}

    # v2.0.1: pipeline_graph validation — fail fast on misconfig before
    # any LLM call. The validator reproduces the three concurrency
    # invariants statically; a config that would violate them at runtime
    # is rejected here. See ``core/pipeline_graph.py:validate``.
    if pipeline_graph is not None:
        from .pipeline_graph import validate as _validate_graph
        _validate_graph(pipeline_graph)
        await _emit(on_event, EventType.AGENT_STEP,
                    step="pipeline_graph_loaded",
                    detail=f"validated {len(pipeline_graph.nodes)} pass nodes "
                           f"(schema v{pipeline_graph.version})")

    # ── Branch initialization (optional) ─────────────────────────────
    # When ``parent_run_id`` is set, load the parent run's stored pass
    # outputs and reuse them up to and including ``branch_from_pass``.
    # Pass 4 + transforms always re-run because they depend on the new
    # prompt. We re-use the AGENT_STEP event (the EventType enum is
    # frozen — see ``docs/EXTRACTION_GOTCHAS.md``) plus add ``parent_*``
    # keys to identify the branch.
    branch_state: dict[str, Any] | None = None
    if opts.parent_run_id is not None:
        branch_from_pass = opts.branch_from_pass
        if branch_from_pass is None or branch_from_pass < 1:
            raise BranchError(
                f"branch_from_pass must be ≥ 1 when parent_run_id is set "
                f"(got {branch_from_pass!r})"
            )
        if branch_from_pass > 3:
            raise BranchError(
                f"branch_from_pass must be ≤ 3 (got {branch_from_pass}); "
                "Pass 4 always re-runs against the new prompt."
            )
        # Resolve parent record; raise a domain error (not IntegrityError)
        # if the parent does not exist so the caller sees a clean failure.
        from ..persistence import runs as _runs_mod
        from ..config import db_path as _default_db_path

        bdb = opts.branch_db_path or _default_db_path()
        parent = _runs_mod.get_run(bdb, opts.parent_run_id)
        if parent is None:
            raise BranchError(
                f"parent run {opts.parent_run_id!r} not found in {bdb}"
            )
        branch_state = {
            "parent_run_id": opts.parent_run_id,
            "branch_from_pass": branch_from_pass,
            "pass1": parent.get("pass1_output") or "",
            "pass2": parent.get("pass2_output") or "",
            "task_type": parent.get("task_type") or "",
            "technique": parent.get("technique") or "precision",
            "parent_enhanced": parent.get("enhanced_prompt") or "",
        }
        await _emit(
            on_event, EventType.AGENT_STEP,
            step="branch_start",
            parent_run_id=opts.parent_run_id,
            parent_pass=branch_from_pass,
            detail=f"branched from {opts.parent_run_id[:8]} @ Pass {branch_from_pass}",
        )

    # ── budgets ─────────────────────────────────────────────────────
    char_budget = DEFAULT_CHAR_BUDGET
    try:
        ctx_tokens = await provider.context_window(model)
        if ctx_tokens:
            char_budget = int(ctx_tokens * 0.75) * 4
    except Exception:  # noqa: BLE001
        char_budget = DEFAULT_CHAR_BUDGET
    if char_budget == DEFAULT_CHAR_BUDGET:
        # Fall back to the model-name heuristics in detect_context_budget.
        # Pass an empty management URL — the function gracefully handles
        # it and skips the API query.
        char_budget = detect_context_budget(model, "http://localhost:0")

    budgets = compute_pass_budgets(char_budget)

    # ── Pass 1 + Pass 2: STRICTLY SERIAL ─────────────────────────────
    # LM Studio (and LM Link → remote GPU) serves one request at a time
    # per model. asyncio.gather here causes server-side queueing →
    # httpx ReadTimeout. DO NOT parallelize.
    # Test: tests/test_concurrency.py::test_pass1_pass2_serial.
    t0 = time.monotonic()
    p1_duration_ms = 0
    p2_duration_ms = 0

    if opts.resume_state is not None:
        # Disambiguation resume — skip Pass 1/2.
        rs = opts.resume_state
        pass1 = rs["pass1"]
        pass2 = rs["pass2"]
        task_type = rs["task_type"]
        technique = rs["technique"]
        session_context = rs.get("session_context", session_context)
        disambig_ctx = rs.get("disambiguation_context", "")
        t1 = rs.get("t1", time.monotonic())
        t0 = rs.get("t0", t0)
        # Carry per-pass durations from the original run so the analytics
        # dashboard sees the same breakdown after resume.
        p1_duration_ms = rs.get("p1_duration_ms", 0)
        p2_duration_ms = rs.get("p2_duration_ms", 0)
        await _emit(on_event, EventType.AGENT_STEP,
                    step="resume", detail="resumed after clarifications")
    elif branch_state is not None and branch_state["branch_from_pass"] >= 2:
        # Branching from Pass 2 or later — reuse parent's Pass 1 + Pass 2
        # analyses verbatim. Pass 3 will run against the NEW prompt, so
        # transforms reflect the user's iteration.
        pass1 = branch_state["pass1"]
        pass2 = branch_state["pass2"]
        task_type = branch_state["task_type"] or coerce_task_type_for_code(
            parse_task_type(pass1), prompt
        )
        technique = branch_state["technique"] or parse_technique(pass2)
        disambig_ctx = ""
        # Re-emit the parent's pass results so the UI status strip lights
        # up correctly without ever calling the provider for them.
        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=1, pass_name=PASS_NAMES[1],
                    content=pass1, model=model,
                    duration_ms=0, task_type=task_type)
        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=2, pass_name=PASS_NAMES[2],
                    content=pass2, model=model,
                    duration_ms=0, technique=technique)
        t1 = time.monotonic()
    else:
        # Pass 1 — Intent Analysis
        await _emit(on_event, EventType.AGENT_PASS_START,
                    pass_number=1, pass_name=PASS_NAMES[1], model=model)

        p1_user = _wrap_with_session(
            session_context, prompt, char_budget // 2, label="prompt",
        )
        # v2.0.1: optional MCP pre-Pass-1 enrichment.
        if mcp_invoker is not None and mcp_pre_pass1:
            mcp_enrichment = await _run_mcp_hooks(
                mcp_invoker, mcp_pre_pass1, on_event=on_event,
            )
            if mcp_enrichment:
                p1_user = (
                    f"{p1_user}\n\n[MCP CONTEXT]\n{mcp_enrichment}\n[END MCP CONTEXT]"
                )
        pass1_messages = [
            {"role": "system", "content": PASS1_SYSTEM},
            {"role": "user", "content": truncate(p1_user, char_budget, "p1")},
        ]
        if reasoning_panel is not None:
            try:
                pass1, p1_panel_tel = await _call_panel(
                    reasoning_panel, pass1_messages,
                    mode=panel_mode, aggregator=panel_aggregator,
                    temperature=temperature,
                    max_tokens=scaled(budgets.analysis, max_tokens_scale),
                    timeout=request_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                await _emit(on_event, EventType.AGENT_ERROR,
                            step="pass1", error=str(exc))
                raise
            panel_telemetry["pass1"] = p1_panel_tel
            if pass1:
                await _emit(on_event, EventType.AGENT_PASS_CHUNK,
                            pass_number=1, token=pass1)
        else:
            pass1_chunks: list[str] = []
            try:
                async for tok in provider.chat_stream(
                    messages=pass1_messages,
                    model=model, temperature=temperature,
                    max_tokens=scaled(budgets.analysis, max_tokens_scale),
                    timeout=request_timeout, idle_timeout=idle_timeout,
                ):
                    pass1_chunks.append(tok)
                    await _emit(on_event, EventType.AGENT_PASS_CHUNK,
                                pass_number=1, token=tok)
            except Exception as exc:  # noqa: BLE001
                await _emit(on_event, EventType.AGENT_ERROR,
                            step="pass1", error=str(exc))
                raise
            pass1 = "".join(pass1_chunks)
        t_after_p1 = time.monotonic()
        p1_duration_ms = int((t_after_p1 - t0) * 1000)

        # Pass 2 — Weakness Detection
        await _emit(on_event, EventType.AGENT_PASS_START,
                    pass_number=2, pass_name=PASS_NAMES[2], model=model)

        pass2_messages = [
            {"role": "system", "content": PASS2_SYSTEM},
            {"role": "user", "content": truncate(prompt, char_budget, "p2")},
        ]
        if reasoning_panel is not None:
            try:
                pass2, p2_panel_tel = await _call_panel(
                    reasoning_panel, pass2_messages,
                    mode=panel_mode, aggregator=panel_aggregator,
                    temperature=temperature,
                    max_tokens=scaled(budgets.analysis, max_tokens_scale),
                    timeout=request_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                await _emit(on_event, EventType.AGENT_ERROR,
                            step="pass2", error=str(exc))
                raise
            panel_telemetry["pass2"] = p2_panel_tel
            if pass2:
                await _emit(on_event, EventType.AGENT_PASS_CHUNK,
                            pass_number=2, token=pass2)
        else:
            pass2_chunks: list[str] = []
            try:
                async for tok in provider.chat_stream(
                    messages=pass2_messages,
                    model=model, temperature=temperature,
                    max_tokens=scaled(budgets.analysis, max_tokens_scale),
                    timeout=request_timeout, idle_timeout=idle_timeout,
                ):
                    pass2_chunks.append(tok)
                    await _emit(on_event, EventType.AGENT_PASS_CHUNK,
                                pass_number=2, token=tok)
            except Exception as exc:  # noqa: BLE001
                await _emit(on_event, EventType.AGENT_ERROR,
                            step="pass2", error=str(exc))
                raise
            pass2 = "".join(pass2_chunks)

        t1 = time.monotonic()
        p2_duration_ms = int((t1 - t_after_p1) * 1000)
        task_type = coerce_task_type_for_code(parse_task_type(pass1), prompt)
        technique = parse_technique(pass2)
        disambig_ctx = ""

        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=1, pass_name=PASS_NAMES[1],
                    content=pass1, model=model,
                    duration_ms=p1_duration_ms,
                    task_type=task_type)
        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=2, pass_name=PASS_NAMES[2],
                    content=pass2, model=model,
                    duration_ms=p2_duration_ms,
                    technique=technique)

        # ── Interactive disambiguation ──────────────────────────────
        if count_weakness_fields(pass2) >= DISAMBIGUATE_THRESHOLD:
            disambig_id = await _maybe_disambiguate(
                prompt, pass1, pass2,
                provider=provider, model=model,
                temperature=temperature, max_tokens_scale=max_tokens_scale,
                request_timeout=request_timeout,
                opts=opts, on_event=on_event,
                task_type=task_type, technique=technique,
                session_context=session_context, t0=t0, t1=t1,
                p1_duration_ms=p1_duration_ms, p2_duration_ms=p2_duration_ms,
                pending_disambig=pending_disambig,
            )
            if disambig_id is not None:
                # Pipeline pauses. Caller resumes via opts.resume_state.
                # Return a sentinel result — the actual result will come
                # from the resumed run.
                return _empty_result(
                    prompt=prompt, model=model, scorer_model=scorer_model,
                    technique=technique, task_type=task_type,
                )

    t2 = t1  # Pass 1/2 finished at the same instant in our timing model.

    # ── Persona detection (optional) ────────────────────────────────
    persona_text: str | None = None
    persona_time_ms = 0
    pass3_system = select_pass3_system(task_type)

    if opts.persona_mode:
        await _emit(on_event, EventType.PERSONA_START, model=model)
        tp0 = time.monotonic()
        try:
            persona_user = (
                f"User prompt:\n{truncate(prompt, char_budget // 4, 'persona-prompt')}\n\n"
                f"Intent analysis:\n{truncate(pass1, char_budget // 4, 'persona-pass1')}\n\n"
                f"Weakness analysis:\n{truncate(pass2, char_budget // 4, 'persona-pass2')}"
            )
            # Streams instead of one-shot chat — same rationale as Pass 4:
            # reasoning-token models (gpt-oss family) reliably return EMPTY
            # content from non-streaming /chat/completions because LM Studio
            # post-filters the reasoning block. Streaming SSE bypasses that
            # filter. See knowledge/lm-studio-models.md §1.
            persona_chunks: list[str] = []
            async for tok in provider.chat_stream(
                messages=[
                    {"role": "system", "content": PERSONA_SYSTEM},
                    {"role": "user", "content": persona_user},
                ],
                model=model, temperature=temperature,
                max_tokens=scaled(budgets.persona, max_tokens_scale),
                timeout=request_timeout, idle_timeout=idle_timeout,
            ):
                persona_chunks.append(tok)
            raw = "".join(persona_chunks)
            tp1 = time.monotonic()
            parsed = parse_persona(raw) or PERSONA_FALLBACK
            persona_text = parsed
            persona_time_ms = int((tp1 - tp0) * 1000)
            t2_effective = tp1
            await _emit(on_event, EventType.PERSONA_RESULT,
                        persona=parsed, raw_output=raw,
                        duration_ms=persona_time_ms, model=model)
            pass3_system = (
                f"You are: {parsed}\n\n{pass3_system}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Persona detection failed, falling back: %s", exc)
            persona_text = PERSONA_FALLBACK
            t2_effective = t2
    else:
        t2_effective = t2

    # ── Pass 3 — Task-aware Rewrite (streamed) ──────────────────────
    pass3_partial = False
    if branch_state is not None and branch_state["branch_from_pass"] >= 3:
        # Branching from Pass 3 — keep parent's enhanced verbatim. Pass 4
        # still runs against the new ``prompt`` so scoring reflects the
        # reused enhancement vs. the new original.
        enhanced = branch_state["parent_enhanced"]
        t3 = time.monotonic()
        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=3, pass_name=PASS_NAMES[3],
                    content=enhanced, model=model, duration_ms=0)
    else:
        await _emit(on_event, EventType.AGENT_PASS_START,
                    pass_number=3, pass_name=PASS_NAMES[3], model=model)

        technique_guidance = TECHNIQUE_GUIDANCE.get(technique, "")
        pass3_user_parts = [
            f"Original prompt:\n{truncate(prompt, char_budget // 3, 'p3-prompt')}\n",
            f"Intent analysis:\n{truncate(pass1, char_budget // 4, 'p3-pass1')}\n",
            f"Weakness analysis:\n{truncate(pass2, char_budget // 4, 'p3-pass2')}\n",
        ]
        if technique_guidance:
            pass3_user_parts.append(technique_guidance + "\n")
        if disambig_ctx:
            pass3_user_parts.append(disambig_ctx + "\n")
        if session_context:
            pass3_user_parts.append(
                "[SESSION CONTEXT]\n"
                + truncate(session_context, char_budget // 2, "p3-session")
                + "\n[END SESSION CONTEXT]\n"
            )

        # v2.0.1: optional MCP pre-Pass-3 enrichment (rewrite tools).
        if mcp_invoker is not None and mcp_pre_pass3:
            mcp_enrichment = await _run_mcp_hooks(
                mcp_invoker, mcp_pre_pass3, on_event=on_event,
            )
            if mcp_enrichment:
                pass3_user_parts.append(
                    f"[MCP CONTEXT]\n{mcp_enrichment}\n[END MCP CONTEXT]\n"
                )

        pass3_user = "\n".join(pass3_user_parts)

        enhanced_chunks: list[str] = []
        try:
            async for tok in provider.chat_stream(
                messages=[
                    {"role": "system", "content": pass3_system},
                    {"role": "user", "content": pass3_user},
                ],
                model=model, temperature=temperature,
                max_tokens=scaled(budgets.rewrite, max_tokens_scale),
                timeout=request_timeout, idle_timeout=idle_timeout,
            ):
                enhanced_chunks.append(tok)
                await _emit(on_event, EventType.AGENT_PASS_CHUNK,
                            pass_number=3, token=tok)
        except Exception as exc:  # noqa: BLE001
            pass3_partial = True
            if not enhanced_chunks:
                # Fall back to original prompt — Pass 4 will be skipped because
                # comparing prompt-vs-prompt wastes a call. Documented in
                # docs/EXTRACTION_GOTCHAS.md §9.
                enhanced_chunks.append(prompt)
            await _emit(on_event, EventType.AGENT_ERROR,
                        step="pass3", error=str(exc))

        enhanced = "".join(enhanced_chunks)
        t3 = time.monotonic()
        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=3, pass_name=PASS_NAMES[3],
                    content=enhanced, model=model,
                    duration_ms=int((t3 - t2_effective) * 1000))

    # ── Pass 4 — Quality Scoring (background task) ──────────────────
    # v2.0.1: model_router auto-selection. If the user didn't explicitly
    # set opts.scorer_model, ask the router to pick a task-aware scorer
    # from the available models. Best-effort: any failure (LM Studio
    # unreachable, no models loaded, router error) silently keeps the
    # early default of `model`.
    if not opts.scorer_model:
        try:
            from ..llm.model_router import select_scorer
            available = await provider.list_models()
            if available:
                routed = select_scorer(task_type, available, preferred=None)
                if routed:
                    scorer_model = routed
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("model_router fallback (%s); keeping %r", exc, scorer_model)

    pass4_task: asyncio.Task | None = None
    pass4_text = ""
    scores: dict[str, int] = dict(P4_DEFAULTS)
    scores_fallback = False

    if pass3_partial and enhanced == prompt:
        # No meaningful enhancement — skip scoring entirely.
        scores_fallback = True
    else:
        async def _run_pass4_bg() -> tuple[str, dict[str, int], float, dict | None]:
            # Pass 4 streams instead of one-shot chat. Reasoning-token
            # models (gpt-oss family) reliably return EMPTY visible
            # content from non-streaming `/chat/completions` because LM
            # Studio post-filters the reasoning block. Streaming SSE
            # bypasses that filter and the four score lines arrive
            # intact. See knowledge/lm-studio-models.md §1.
            t4_start = time.monotonic()
            p4_user = (
                "Original:\n"
                f"{truncate(prompt, char_budget // 3, 'p4-orig')}\n\n"
                "Enhanced:\n"
                f"{truncate(enhanced, char_budget // 3, 'p4-enh')}"
            )
            p4_messages = [
                {"role": "system", "content": PASS4_SYSTEM},
                {"role": "user", "content": p4_user},
            ]
            if reasoning_panel is not None:
                raw, p4_tel = await _call_panel(
                    reasoning_panel, p4_messages,
                    mode=panel_mode, aggregator=panel_aggregator,
                    temperature=temperature,
                    max_tokens=scaled(budgets.score, max_tokens_scale),
                    timeout=request_timeout,
                )
                return raw, parse_scores(raw), time.monotonic() - t4_start, p4_tel
            chunks: list[str] = []
            async for tok in provider.chat_stream(
                messages=p4_messages,
                model=scorer_model, temperature=temperature,
                max_tokens=scaled(budgets.score, max_tokens_scale),
                timeout=request_timeout, idle_timeout=idle_timeout,
            ):
                chunks.append(tok)
            raw = "".join(chunks)
            return raw, parse_scores(raw), time.monotonic() - t4_start, None

        pass4_task = asyncio.create_task(_run_pass4_bg())

    # ── AWAIT PASS 4 BEFORE MAGNITUDE / SoT ─────────────────────────
    # LM Studio serves one request at a time per model. Pass 4 + a
    # streaming Magnitude/SoT in parallel = server-side queueing →
    # ReadTimeout. DO NOT change the ordering here.
    # Test: tests/test_concurrency.py::test_pass4_awaited_before_magnitude.
    t4_dur_ms = 0
    if pass4_task is not None:
        try:
            pass4_text, scores, p4_dur, p4_tel = await pass4_task
            t4_dur_ms = int(p4_dur * 1000)
            scores_fallback = not bool(pass4_text)
            if p4_tel is not None:
                panel_telemetry["pass4"] = p4_tel
        except Exception as exc:  # noqa: BLE001
            scores_fallback = True
            await _emit(on_event, EventType.AGENT_ERROR,
                        step="pass4", error=str(exc))
        finally:
            pass4_task = None  # mark as already consumed — load-bearing!

    if not scores_fallback:
        await _emit(on_event, EventType.AGENT_PASS_RESULT,
                    pass_number=4, pass_name=PASS_NAMES[4],
                    content=pass4_text, model=scorer_model,
                    duration_ms=t4_dur_ms, scores=scores)

    # ── Self-correction retry (if improvement is low) ───────────────
    if (
        not scores_fallback
        and not pass3_partial
        and opts.resume_state is None
        and scores.get("improvement", 0) < 20
    ):
        await _emit(on_event, EventType.AGENT_STEP,
                    step="self_correction", detail="improvement<20, retrying")
        retry_user = (
            f"Critique of previous attempt:\n{pass4_text}\n\n"
            f"Original prompt:\n{prompt}\n\n"
            f"Previous enhanced prompt:\n{enhanced}\n\n"
            "Rewrite the prompt addressing the critique. "
            "Output ONLY the rewritten prompt."
        )
        retry_chunks: list[str] = []
        try:
            async for tok in provider.chat_stream(
                messages=[
                    {"role": "system", "content": pass3_system},
                    {"role": "user", "content": retry_user},
                ],
                model=model, temperature=temperature,
                max_tokens=scaled(budgets.rewrite, max_tokens_scale),
                timeout=request_timeout, idle_timeout=idle_timeout,
            ):
                retry_chunks.append(tok)
                await _emit(on_event, EventType.AGENT_PASS_CHUNK,
                            pass_number=3, token=tok)
            if retry_chunks:
                enhanced = "".join(retry_chunks)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Self-correction retry failed: %s", exc)

    # ── Magnitude transform (after Pass 4, optional) ────────────────
    magnitude_output = ""
    if opts.magnitude_mode:
        await _emit(on_event, EventType.MAGNITUDE_START)
        mag_chunks: list[str] = []
        try:
            async for tok in provider.chat_stream(
                messages=[
                    {"role": "system", "content": MAGNITUDE_SYSTEM_PROMPT},
                    {"role": "user", "content": truncate(enhanced, char_budget, "mag")},
                ],
                model=model, temperature=temperature,
                max_tokens=scaled(budgets.magnitude, max_tokens_scale),
                timeout=request_timeout, idle_timeout=idle_timeout,
            ):
                mag_chunks.append(tok)
                await _emit(on_event, EventType.MAGNITUDE_CHUNK, token=tok)
        except Exception as exc:  # noqa: BLE001
            await _emit(on_event, EventType.MAGNITUDE_ERROR, error=str(exc))
        magnitude_output = "".join(mag_chunks)
        await _emit(on_event, EventType.MAGNITUDE_DONE, content=magnitude_output)

    # ── Skeleton-of-Thought (after Pass 4, optional) ────────────────
    sot_output = ""
    if opts.sot_mode:
        await _emit(on_event, EventType.SOT_START)
        sot_chunks: list[str] = []
        try:
            async for tok in provider.chat_stream(
                messages=[
                    {"role": "system", "content": SOT_SYSTEM_PROMPT},
                    {"role": "user", "content": truncate(enhanced, char_budget, "sot")},
                ],
                model=model, temperature=temperature,
                max_tokens=scaled(budgets.sot, max_tokens_scale),
                timeout=request_timeout, idle_timeout=idle_timeout,
            ):
                sot_chunks.append(tok)
                await _emit(on_event, EventType.SOT_CHUNK, token=tok)
        except Exception as exc:  # noqa: BLE001
            await _emit(on_event, EventType.SOT_ERROR, error=str(exc))
        sot_output = "".join(sot_chunks)
        await _emit(on_event, EventType.SOT_DONE, content=sot_output)

    # ── Summary + Done ──────────────────────────────────────────────
    pass_times_ms = {
        "pass1": p1_duration_ms,
        "pass2": p2_duration_ms,
        "pass3": int((t3 - t2_effective) * 1000),
        "pass4": t4_dur_ms,
    }
    if persona_time_ms:
        pass_times_ms["persona"] = persona_time_ms

    await _emit(on_event, EventType.AGENT_PIPELINE_SUMMARY,
                total_duration_ms=int((time.monotonic() - t0) * 1000),
                pass_times_ms=pass_times_ms,
                technique=technique, task_type=task_type,
                scores=scores, model=model, scorer_model=scorer_model,
                persona=persona_text)

    await _emit(on_event, EventType.ENHANCEMENT_SCORE,
                scores=scores, scores_fallback=scores_fallback,
                pass_times_ms=pass_times_ms, scorer_model=scorer_model)

    # Build the final record so callers can persist it.
    record = RunRecord(
        prompt=prompt,
        enhanced_prompt=enhanced,
        task_type=task_type,
        technique=technique,
        persona=persona_text,
        pass1_output=pass1,
        pass2_output=pass2,
        pass4_output=pass4_text,
        magnitude_output=magnitude_output,
        sot_output=sot_output,
        pass_times_ms=pass_times_ms,
        model=model,
        scorer_model=scorer_model,
        temperature=temperature,
        max_tokens_scale=max_tokens_scale,
        scores=scores,
        scores_fallback=scores_fallback,
        pass3_partial=pass3_partial,
        session_id=opts.session_id,
        parent_run_id=opts.parent_run_id,
        parent_pass=opts.branch_from_pass,
    )

    await _emit(on_event, EventType.AGENT_DONE,
                result=enhanced, technique=technique, task_type=task_type,
                scores=scores, scores_fallback=scores_fallback,
                run_id=record.id)

    extras_payload: dict[str, Any] = {"_record": record}
    if panel_telemetry:
        extras_payload["panel"] = panel_telemetry
    return PipelineResult(
        result=enhanced, technique=technique, task_type=task_type,
        scores=scores, scores_fallback=scores_fallback,
        pass3_partial=pass3_partial, persona=persona_text,
        magnitude_output=magnitude_output, sot_output=sot_output,
        pass_times_ms=pass_times_ms, model=model,
        scorer_model=scorer_model, run_id=record.id,
        extras=extras_payload,
    )


# ── helpers ─────────────────────────────────────────────────────────

async def _call_panel(
    panel: "ReasoningPanel",
    messages: list[dict[str, Any]],
    *,
    mode: str,
    aggregator: str,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    """Run a panel consultation and shape telemetry for ``extras["panel"]``.

    Mirrors the telemetry shape used by ``development.stages.reviewer``:
    ``{"primary": <content>, "partners": [{"name", "content", "ms",
    "error"}, ...]}``. The aggregated text is what the pipeline parses
    as if it had come from a single ``provider.chat`` call.
    """
    result = await panel.consult(
        messages,
        mode=mode,
        aggregator=aggregator,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    telemetry = {
        "primary": result.primary.content,
        "partners": [
            {
                "name": p.slot_name,
                "content": p.content,
                "ms": p.duration_ms,
                "error": p.error,
            }
            for p in result.partners
        ],
    }
    return result.aggregated, telemetry


async def _run_mcp_hooks(
    invoker: "MCPToolInvoker",
    hooks: list[dict],
    *,
    on_event: OnEvent | None,
) -> str:
    """Run a list of MCP tool calls via ``invoker`` and concatenate results.

    Each ``hook`` is a dict with keys ``server`` (str), ``tool`` (str),
    ``args`` (dict). The invoker emits ``MCP_TOOL_INVOKED`` /
    ``MCP_TOOL_RESULT`` events for each call so the UI can surface
    progress without the pipeline knowing the protocol.

    Failures are swallowed (logged at WARNING) so a misbehaving MCP
    server can't break a pipeline run. The function never raises.
    Returns the concatenated tool-result text or empty string.
    """
    if not hooks:
        return ""

    chunks: list[str] = []
    for hook in hooks:
        server = hook.get("server")
        tool = hook.get("tool")
        args = hook.get("args") or {}
        if not (server and tool):
            logger.warning("Skipping malformed MCP hook %r — needs server+tool", hook)
            continue
        try:
            result = await invoker.invoke_with_events(
                server=server, tool=tool, args=args, on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001 — never break the pipeline
            logger.warning("MCP hook %s/%s failed: %s", server, tool, exc)
            continue
        # Result shape varies by tool; render best-effort.
        if isinstance(result, dict):
            content = result.get("content") or result.get("result") or result
        else:
            content = result
        chunks.append(f"--- {server}/{tool} ---\n{content}")
    return "\n\n".join(chunks)


def _wrap_with_session(session_context: str, prompt: str,
                       budget: int, label: str) -> str:
    """Wrap prompt in [SESSION CONTEXT] markers if session_context is non-empty."""
    if not session_context:
        return prompt
    truncated = truncate(session_context, budget, label)
    return (
        "[SESSION CONTEXT]\n"
        f"{truncated}\n"
        "[END SESSION CONTEXT]\n\n"
        "[CURRENT REQUEST]\n"
        f"{prompt}"
    )


def _empty_result(*, prompt: str, model: str, scorer_model: str,
                  technique: str, task_type: str) -> PipelineResult:
    """Sentinel returned when the pipeline pauses for disambiguation."""
    return PipelineResult(
        result="",                       # caller knows to wait for resume
        technique=technique, task_type=task_type,
        scores=dict(P4_DEFAULTS), scores_fallback=True,
        pass3_partial=False, persona=None,
        magnitude_output="", sot_output="",
        pass_times_ms={}, model=model, scorer_model=scorer_model,
        run_id="", extras={"paused": True},
    )


def build_resume_state(
    snapshot: dict[str, Any],
    answers: dict[str, str],
) -> dict[str, Any]:
    """Construct a resume_state dict from a pending_disambig snapshot.

    ``snapshot`` is the entry stored in the ``pending_disambig`` dict
    when the pipeline paused (see ``_maybe_disambiguate``). ``answers``
    maps question ids (``Q1``, ``Q2``, …) to the user's answer text.

    Pass the returned dict as ``opts.resume_state`` on the next
    ``run_pipeline`` call to skip Pass 1/2 and go straight to Pass 3
    with the clarifications injected into the user message.
    """
    questions_by_id = snapshot.get("questions", {})
    answer_lines = [
        f"- {questions_by_id.get(q_id, q_id)}: {ans}"
        for q_id, ans in answers.items()
    ]
    answer_context = (
        "[USER CLARIFICATIONS]\n"
        + "\n".join(answer_lines)
        + "\n[END USER CLARIFICATIONS]"
    ) if answer_lines else ""
    return {
        "pass1": snapshot["pass1"],
        "pass2": snapshot["pass2"],
        "task_type": snapshot["task_type"],
        "technique": snapshot["technique"],
        "session_context": snapshot.get("session_context", ""),
        "t0": snapshot["t0"],
        "t1": snapshot["t1"],
        "p1_duration_ms": snapshot.get("p1_duration_ms", 0),
        "p2_duration_ms": snapshot.get("p2_duration_ms", 0),
        "disambiguation_context": answer_context,
    }


async def _maybe_disambiguate(
    prompt: str, pass1: str, pass2: str,
    *,
    provider: ChatProvider, model: str,
    temperature: float, max_tokens_scale: float, request_timeout: float,
    opts: PipelineOptions, on_event: OnEvent | None,
    task_type: str, technique: str, session_context: str,
    t0: float, t1: float,
    p1_duration_ms: int = 0, p2_duration_ms: int = 0,
    pending_disambig: dict[str, dict],
) -> str | None:
    """Generate clarification questions; pause if any are produced.

    Returns the ``disambig_id`` if the pipeline paused, else ``None`` so
    the caller proceeds to Pass 3.
    """
    import secrets

    user_msg = (
        f"Original prompt:\n{prompt}\n\n"
        f"Weakness analysis:\n{pass2}"
    )
    try:
        raw = await provider.chat(
            messages=[
                {"role": "system", "content": DISAMBIGUATE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            model=model, temperature=temperature,
            max_tokens=scaled(400, max_tokens_scale),
            timeout=request_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Disambiguation generation failed, skipping: %s", exc)
        return None

    questions = parse_disambiguate_questions(raw)
    if not questions:
        return None

    disambig_id = secrets.token_hex(8)
    pending_disambig[disambig_id] = {
        "prompt": prompt,
        "scorer_model": opts.scorer_model,
        "magnitude_mode": opts.magnitude_mode,
        "persona_mode": opts.persona_mode,
        "sot_mode": opts.sot_mode,
        "session_id": opts.session_id,
        "pass1": pass1, "pass2": pass2,
        "task_type": task_type, "technique": technique,
        "session_context": session_context,
        "t0": t0, "t1": t1,
        "p1_duration_ms": p1_duration_ms,
        "p2_duration_ms": p2_duration_ms,
        "questions": {f"Q{i + 1}": q["question"] for i, q in enumerate(questions)},
    }
    await _emit(on_event, EventType.AGENT_DISAMBIGUATE,
                disambig_id=disambig_id, questions=questions)
    return disambig_id
