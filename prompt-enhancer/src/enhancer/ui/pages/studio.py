"""Studio page — the heart of the Desktop Studio.

Layout (mirrors the redesigned Agent Loop mod):

    ┌─ Status strip (9 nodes) ─────────────────────────────┐
    ├─ Toolbar: Scorer model | Sessions | Refresh ─────────┤
    ├─ Tabs: Log | Magnitude | SoT | Settings ─────────────┤
    │  ▼ active tab body (pass_cards stream into Log) ─────┤
    └─ Input: prompt textarea + Enhance ───────────────────┘

Now uses the full component set:
* :mod:`status_strip` for the 9-node tracker.
* :mod:`pass_card` for each pass result (replaces plain ui.label entries).
* :mod:`score_chips` rendered inline by the Pass 4 card.
* :mod:`session_drawer` for session continuity on the right edge.
* :mod:`diff_view` for original-vs-enhanced inspection.
"""

from __future__ import annotations

import time
from typing import Any

from nicegui import ui

import asyncio

from ...config import db_path, jsonl_log_path, load
from ...core.events import EventType
from ...core.pipeline import PipelineOptions, build_resume_state, run_pipeline
from ...llm.panel_config import (
    build_panel as build_reasoning_panel,
    load_panel_config,
)
from ...llm.registry import get_provider
from ...persistence import runs as runs_module
from ..components.activity_panel import render_activity_panel
from ..components.diff_view import render_diff
from ..components.pass_card import render_pass_card
from ..components.round_robin_handoff import post_persona_handoff, post_review
from ..components.score_chips import render_score_chips
from ..components.session_drawer import SessionDrawer, session_context_for
from ..components.status_strip import StatusStrip


# Map pass_number → status-strip node key
_STEP_TO_NODE = {1: "pass1", 2: "pass2", 3: "pass3", 4: "pass4"}


def _format_clock(seconds: float) -> str:
    """``87.4 → "1m 27s"``; sub-minute returns ``"45s"``."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _sidebar() -> None:
    with ui.left_drawer(value=True, fixed=True).classes("bg-[var(--bg-2)] p-4"):
        ui.label("🪄  Prompt Enhancer").classes("text-h6 text-white")
        ui.separator().classes("my-2")
        ui.link("Studio", "/").classes("text-white")
        ui.link("History", "/history").classes("text-white")
        ui.link("Analytics", "/analytics").classes("text-white")
        ui.link("Compare", "/compare").classes("text-white")
        ui.link("Templates", "/templates").classes("text-white")
        ui.link("Settings", "/settings").classes("text-white")


def render() -> None:  # noqa: C901, PLR0915 — page assembly
    settings = load()
    _sidebar()

    # Read branch query params (set by the History page's "Branch from this
    # run" button — e.g. /?branch_from=abc12345&pass=2). When present, we
    # preload the parent's prompt into the input so the user can edit it
    # before clicking Enhance.
    try:
        from nicegui import context as _ng_ctx
        _query = dict(_ng_ctx.client.request.query_params)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — older NiceGUI builds, fall back
        _query = {}
    branch_preload_id = _query.get("branch_from")
    branch_preload_pass = _query.get("pass")

    # ── Right-side session drawer (toggleable) ──────────────────────
    drawer = SessionDrawer(db_path())
    drawer.render()

    # ── Top bar
    with ui.row().classes("w-full items-center justify-between p-3"):
        ui.label("Studio").classes("text-h5 text-white")
        with ui.row().classes("gap-2 items-center"):
            scorer_select = ui.select(
                options=[], label="Scorer model",
            ).classes("min-w-[260px]")
            ui.button("Refresh models", icon="refresh",
                      on_click=lambda: _refresh_models(scorer_select)).props("flat")
            session_status = ui.label("session: —").classes("text-caption text-grey")
            ui.button("Sessions", icon="folder",
                      on_click=lambda: ui.run_javascript(
                          "document.querySelectorAll('.q-drawer--right').forEach("
                          "d => d.classList.toggle('q-drawer--open'))"
                      )).props("flat")

    drawer.on_change(lambda sid: session_status.set_text(
        f"session: {sid[:8]}…" if sid else "session: —"
    ))

    # ── Status strip + live streaming progress line
    with ui.element("div").classes("studio-card"):
        strip = StatusStrip()
        # Live "still streaming…" row — visible only while a pass is running.
        # Polled every 0.5s by a ui.timer so the elapsed-clock doesn't lag.
        with ui.row().classes("gap-3 items-center mt-1"):
            live_pass_lbl = ui.label("").classes("text-caption text-grey")
            live_tokens_lbl = ui.label("").classes("text-caption text-grey")
            live_time_lbl = ui.label("").classes("text-caption text-grey")
            live_rate_lbl = ui.label("").classes("text-caption text-grey")
        # Branch badge — visible only when this run was forked from a parent.
        branch_badge = ui.label("").classes("text-caption").style(
            "color: var(--accent); display: none;"
        )
        # Panel badge — visible only when a Reasoning Panel is wired into
        # the next run. Updated each time we read panel.toml just before
        # calling run_pipeline. (See _run_pipeline_action below.)
        panel_badge = ui.label("").classes("text-caption").style(
            "color: var(--accent); display: none;"
        )

    # ── Tabs
    with ui.tabs().classes("w-full") as tabs:
        log_tab = ui.tab("Log")
        mag_tab = ui.tab("Magnitude")
        sot_tab = ui.tab("SoT")
        settings_tab = ui.tab("Settings")

    state: dict[str, Any] = {
        "log_container": None,
        "p3_card_container": None,  # holds the live-streaming Pass 3 card
        "p3_buffer": "",
        "magnitude": "",
        "sot": "",
        "running": False,
        "last_result": None,
        "pass_meta": {},  # pass_number -> {start_ts, model}
        # Disambiguation pause state — set when agent_disambiguate fires.
        "pending_disambig": {},
        "captured_disambig": {},
        # Live-streaming indicator — what the timer polls.
        "live": {"phase": "", "tokens": 0, "start_ts": None},
        # Last completed run id — used as the parent for "Branch from here"
        # buttons that appear on each pass card.
        "last_run_id": None,
        # Pending branch request, set by clicking ↗ on a pass card or by
        # query params from the History page. Cleared after Enhance fires.
        "branch": None,  # {"parent_run_id": str, "branch_from_pass": int}
    }
    # Apply the History-page preload, if any.
    if branch_preload_id and branch_preload_pass:
        try:
            state["branch"] = {
                "parent_run_id": branch_preload_id,
                "branch_from_pass": int(branch_preload_pass),
            }
        except (TypeError, ValueError):
            pass

    def _refresh_live() -> None:
        """Polled every 0.5 s; updates the streaming-progress labels.

        Reading from ``state["live"]`` rather than emitting on every chunk
        keeps the UI cheap when Pass 3 streams ~200 tokens/sec.
        """
        live = state["live"]
        if not live["phase"] or live["start_ts"] is None:
            for lbl in (live_pass_lbl, live_tokens_lbl, live_time_lbl, live_rate_lbl):
                lbl.set_text("")
            return
        elapsed = max(time.monotonic() - live["start_ts"], 0.001)
        rate = live["tokens"] / elapsed
        live_pass_lbl.set_text(f"▶ {live['phase']}")
        live_tokens_lbl.set_text(f"{live['tokens']} tokens")
        live_time_lbl.set_text(_format_clock(elapsed))
        live_rate_lbl.set_text(f"{rate:.1f} tok/s")

    ui.timer(0.5, _refresh_live)

    with ui.tab_panels(tabs, value=log_tab).classes("w-full"):
        # Log tab — pass cards stream into here
        with ui.tab_panel(log_tab):
            state["log_container"] = ui.column().classes("w-full gap-2")
            with ui.element("div").classes("studio-card"):
                ui.label("Final enhanced prompt").classes("text-caption text-grey")
                final_md = ui.markdown("_(run a prompt to see output)_")
                final_scores_row = ui.row().classes("gap-1")
                # Round-robin handoff: button + inline verdict panel.
                # Hidden until a run completes (state["last_result"] set).
                with ui.row().classes("gap-2 items-center mt-2") as rr_row:
                    rr_btn = ui.button(
                        "→ Round Robin", icon="reviews",
                    ).props("flat color=primary")
                    # Persona handoff sits next to the review button. Distinct
                    # color (secondary) and icon (groups) make it visually
                    # different despite the shared "→ Round Robin" prefix.
                    rr_persona_btn = ui.button(
                        "→ Round Robin (Personas)", icon="groups",
                    ).props("flat color=secondary")
                    # Development handoff: builds a stack-app from the
                    # enhanced prompt. Distinct icon (build) + accent color.
                    dev_stack_input = ui.input(
                        placeholder="stack hint (e.g. fastapi)",
                    ).props("dense outlined").classes("w-32")
                    dev_build_btn = ui.button(
                        "→ Development", icon="build",
                    ).props("flat color=accent")
                    rr_status = ui.label("").classes("text-caption text-grey")
                rr_row.style("display: none;")
                rr_verdict_container = ui.column().classes("w-full mt-1")
                # Dual-persona display — populated post-run when both
                # primary and partner personas are available.
                persona_pair_container = ui.column().classes("w-full mt-2")
            with ui.expansion("Original ↔ Enhanced diff", icon="compare").classes("w-full"):
                diff_container = ui.column()

        # Magnitude tab
        with ui.tab_panel(mag_tab):
            mag_md = ui.markdown("_(magnitude transform off — enable in Settings)_")

        # SoT tab
        with ui.tab_panel(sot_tab):
            sot_md = ui.markdown("_(skeleton-of-thought off — enable in Settings)_")

        # Settings tab — per-run knobs
        with ui.tab_panel(settings_tab):
            with ui.column().classes("gap-3 max-w-[640px]"):
                with ui.row().classes("gap-4"):
                    persona_sw = ui.switch("Persona mode", value=False)
                    magnitude_sw = ui.switch("Magnitude", value=False)
                    sot_sw = ui.switch("Skeleton-of-Thought", value=False)
                with ui.row().classes("gap-4 items-center"):
                    # Partner-persona is conceptually a child of persona-mode:
                    # disable when the parent is off so the dependency is
                    # visually obvious. (Bound via bind_enabled_from.)
                    complement_persona_sw = ui.switch(
                        "Generate partner persona (for Round Robin)",
                        value=False,
                    )
                    complement_persona_sw.bind_enabled_from(
                        persona_sw, "value",
                    )
                ui.label("Temperature").classes("text-caption")
                temp_slider = ui.slider(min=0.0, max=2.0, step=0.05, value=settings.temperature)
                ui.label("").bind_text_from(
                    temp_slider, "value", backward=lambda v: f"= {v:.2f}",
                ).classes("text-caption text-grey")
                ui.label("Max-tokens scale").classes("text-caption")
                tokens_slider = ui.slider(
                    min=0.3, max=3.0, step=0.1, value=settings.max_tokens_scale,
                )
                ui.label("").bind_text_from(
                    tokens_slider, "value", backward=lambda v: f"= {v:.1f}×",
                ).classes("text-caption text-grey")

    # ── Input + Run button
    with ui.element("div").classes("studio-card w-full"):
        prompt_input = ui.textarea(
            label="Your prompt",
            placeholder="Make me a chatbot for…",
        ).classes("w-full").props("rows=3 dense outlined")
        with ui.row().classes("gap-2 mt-2"):
            run_btn = ui.button("Enhance", icon="auto_fix_high")
            ui.button("Clear", on_click=lambda: prompt_input.set_value("")).props("flat")

    # ── Live Activity (cross-umbrella feed) — collapsible expansion at
    # the bottom of the page. Polls all three siblings every 2 s; renders
    # merged stream so the user can SEE handoffs / runs / builds without
    # tailing logs. See components/activity_panel.py for the contract.
    render_activity_panel()

    # If we arrived via History page's "Branch from this run" button,
    # preload the parent's prompt so the user can edit then Enhance.
    if state.get("branch") is not None:
        try:
            parent = runs_module.get_run(
                db_path(), state["branch"]["parent_run_id"]
            )
        except Exception:  # noqa: BLE001
            parent = None
        if parent:
            prompt_input.set_value(parent.get("prompt") or "")
            branch_badge.set_text(
                f"Will branch from {state['branch']['parent_run_id'][:8]} "
                f"@ Pass {state['branch']['branch_from_pass']} on next Enhance"
            )
            branch_badge.style("color: var(--accent); display: inline;")
        else:
            state["branch"] = None

    # ── helpers ─────────────────────────────────────────────────────

    async def _refresh_models_async() -> None:
        provider = get_provider(load())
        try:
            models = await provider.list_models()
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"list_models failed: {exc}", color="negative")
            return
        scorer_select.options = models
        scorer_select.value = settings.default_model or (models[0] if models else None)
        scorer_select.update()

    def _refresh_models(_):
        ui.timer(0.01, _refresh_models_async, once=True)

    def _on_branch_click(pass_number: int) -> None:
        """Click handler for ↗ Branch from here buttons on pass cards.

        Stages a branch request that the next ``Enhance`` press will
        fulfil. The parent is whichever run finished most recently.
        """
        parent_id = state.get("last_run_id")
        if not parent_id:
            ui.notify(
                "No completed run yet — finish a run first, then branch.",
                color="warning",
            )
            return
        state["branch"] = {
            "parent_run_id": parent_id,
            "branch_from_pass": pass_number,
        }
        branch_badge.set_text(
            f"Will branch from {parent_id[:8]} @ Pass {pass_number} on next Enhance"
        )
        branch_badge.style("color: var(--accent); display: inline;")
        ui.notify(
            f"Edit the prompt then click Enhance — will fork from "
            f"{parent_id[:8]} @ Pass {pass_number}.",
            color="info",
        )

    def _render_verdict(verdict: dict[str, Any]) -> None:
        """Render a ReviewVerdict {decision, summary, issues, regenerate}."""
        rr_verdict_container.clear()
        decision = str(verdict.get("decision", "?")).upper()
        summary = str(verdict.get("summary", ""))
        issues = verdict.get("issues") or []
        regenerate = bool(verdict.get("regenerate", False))
        # Color the decision chip — green for pass-y verdicts, amber otherwise.
        good = decision in {"PASS", "OK", "APPROVE", "APPROVED", "GO"}
        with rr_verdict_container:
            with ui.element("div").classes("studio-card"):
                with ui.row().classes("gap-2 items-center"):
                    ui.label(f"Round-Robin: {decision}").classes(
                        "text-body1 text-white"
                    ).style(
                        f"color: {'#4ade80' if good else '#fbbf24'};"
                    )
                    if regenerate:
                        ui.label("regenerate=true").classes(
                            "text-caption"
                        ).style("color: #f87171;")
                if summary:
                    ui.label(summary).classes("text-body2 text-grey")
                if issues:
                    ui.label("Issues:").classes("text-caption mt-1")
                    with ui.column().classes("gap-0 ml-3"):
                        for item in issues:
                            ui.label(f"• {item}").classes("text-caption text-grey")

    async def _on_round_robin_click() -> None:
        result = state.get("last_result")
        if result is None:
            ui.notify("No completed run yet.", color="warning")
            return
        rr_btn.disable()
        rr_status.set_text("Reviewing…")
        rr_verdict_container.clear()
        try:
            outcome = await post_review(
                original_prompt=(prompt_input.value or "").strip(),
                enhanced=getattr(result, "result", "") or "",
            )
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors
            ui.notify(f"Round-robin handoff failed: {exc}", color="negative")
            rr_status.set_text("")
            rr_btn.enable()
            return

        rr_status.set_text("")
        rr_btn.enable()

        if outcome.status == "ok" and outcome.verdict is not None:
            _render_verdict(outcome.verdict)
            ui.notify("Round-robin verdict received.", color="positive")
        elif outcome.status == "peer_missing":
            ui.notify(outcome.error, color="warning")
        elif outcome.status == "unreachable":
            ui.notify(
                f"Round-robin unreachable: {outcome.error}", color="negative"
            )
        else:  # http_error or anything else
            ui.notify(
                f"Round-robin error: {outcome.error}", color="negative"
            )

    rr_btn.on_click(_on_round_robin_click)

    def _render_persona_pair(
        alpha: str, bravo: str | None
    ) -> None:
        """Render Persona A (primary) and, if present, Persona B (partner).

        Distinct labels make the role explicit so the user knows which is
        which when the two get shipped to round-robin.
        """
        persona_pair_container.clear()
        if not alpha and not bravo:
            return
        with persona_pair_container:
            with ui.element("div").classes("studio-card"):
                ui.label("Persona A (primary)").classes(
                    "text-caption text-grey"
                )
                ui.markdown(f"```\n{alpha or ''}\n```")
            if bravo:
                with ui.element("div").classes("studio-card"):
                    ui.label("Persona B (partner)").classes(
                        "text-caption text-grey"
                    )
                    ui.markdown(f"```\n{bravo}\n```")

    async def _send_personas_to_round_robin(*, silent_on_skip: bool) -> None:
        """Single shared path for manual button + auto-fire-on-completion.

        ``silent_on_skip=True`` is for auto-fire: when there's nothing
        useful to send (no run yet, no Persona A), bail without toasts.
        ``silent_on_skip=False`` is for the manual button: warn the user
        why nothing happened so a click never feels broken.
        """
        result = state.get("last_result")
        if result is None:
            if not silent_on_skip:
                ui.notify("No completed run yet.", color="warning")
            return
        alpha = getattr(result, "persona", None) or ""
        if not alpha:
            if not silent_on_skip:
                ui.notify(
                    "No Persona A — enable Persona mode and re-run before "
                    "handing off.",
                    color="warning",
                )
            return
        bravo = getattr(result, "persona_partner", None) or ""
        if not bravo and not silent_on_skip:
            ui.notify(
                "Sending Persona A only; Bravo will keep its existing "
                "persona.",
                color="warning",
            )

        rr_persona_btn.disable()
        rr_status.set_text("Handing off personas…")
        try:
            outcome = await post_persona_handoff(
                theme=getattr(result, "result", "") or "",
                alpha_persona=alpha,
                bravo_persona=bravo,
            )
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors
            ui.notify(
                f"Persona handoff failed: {exc}", color="negative"
            )
            rr_status.set_text("")
            rr_persona_btn.enable()
            return

        rr_status.set_text("")
        rr_persona_btn.enable()

        if outcome.status == "ok":
            ui.notify("Personas delivered to Round Robin.", color="positive")
        elif outcome.status == "peer_missing":
            # On auto-fire, this is informational not actionable — the user
            # didn't ask for the handoff to happen.
            color = "info" if silent_on_skip else "warning"
            ui.notify(outcome.error, color=color)
        elif outcome.status == "unreachable":
            ui.notify(
                f"Round-robin unreachable: {outcome.error}",
                color="negative",
            )
        else:  # http_error or anything else
            ui.notify(
                f"Round-robin error: {outcome.error}", color="negative"
            )

    async def _on_round_robin_personas_click() -> None:
        await _send_personas_to_round_robin(silent_on_skip=False)

    # Exposed on state so the post-run hook can fire it without a click.
    state["_send_personas_to_round_robin"] = _send_personas_to_round_robin

    rr_persona_btn.on_click(_on_round_robin_personas_click)

    async def _on_dev_build_click() -> None:
        """Kick off a development build with the enhanced prompt as goal."""
        result = state.get("last_result")
        if result is None:
            ui.notify("No completed run yet.", color="warning")
            return
        goal = getattr(result, "result", "") or ""
        if not goal.strip():
            ui.notify("Enhanced prompt is empty — nothing to build.",
                      color="warning")
            return
        stack_hint = (dev_stack_input.value or "").strip() or None

        dev_build_btn.disable()
        rr_status.set_text("Sending to development…")
        try:
            from ..components.round_robin_handoff import post_dev_build
            outcome = await post_dev_build(
                goal=goal, stack_hint=stack_hint,
            )
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors
            ui.notify(f"Development handoff failed: {exc}", color="negative")
            rr_status.set_text("")
            dev_build_btn.enable()
            return

        rr_status.set_text("")
        dev_build_btn.enable()

        if outcome.status == "ok":
            ui.notify(
                "Build kicked off in development. "
                "Watch progress in the Live Activity panel below or open "
                "http://127.0.0.1:8767 for development's own UI.",
                color="positive", multi_line=True, close_button="OK",
            )
        elif outcome.status == "peer_missing":
            ui.notify(outcome.error, color="warning")
        elif outcome.status == "unreachable":
            ui.notify(
                f"Development unreachable: {outcome.error}. "
                "Run Setup.bat / Start.bat from APL root to boot it.",
                color="negative", multi_line=True,
            )
        else:  # http_error or anything else
            ui.notify(
                f"Development error: {outcome.error}", color="negative",
            )

    dev_build_btn.on_click(_on_dev_build_click)

    def _add_pass_card(**kwargs) -> ui.element:
        # Wire ↗ Branch from here onto every completed pass 1-3 card.
        kwargs.setdefault("on_branch", _on_branch_click)
        with state["log_container"]:
            return render_pass_card(**kwargs)

    async def _on_event(event_type: EventType, **payload):  # noqa: C901
        name = event_type.value if hasattr(event_type, "value") else str(event_type)

        if name == EventType.AGENT_PASS_START.value:
            n = payload.get("pass_number")
            key = _STEP_TO_NODE.get(n)
            if key:
                strip.set(key, "running")
            state["pass_meta"][n] = {"model": payload.get("model", "")}
            state["live"] = {
                "phase": payload.get("pass_name") or f"Pass {n}",
                "tokens": 0,
                "start_ts": time.monotonic(),
            }
            # Pass 3 needs a streaming placeholder card the chunks update.
            if n == 3:
                with state["log_container"]:
                    state["p3_card_container"] = ui.element("div").classes(
                        "studio-card"
                    ).style("border-left: 3px solid var(--accent);")
                    with state["p3_card_container"]:
                        ui.label(f"▶ {payload.get('pass_name')}").classes(
                            "text-body1 text-white"
                        )
                        state["p3_md"] = ui.markdown("_(streaming…)_")

        elif name == EventType.AGENT_PASS_CHUNK.value:
            state["live"]["tokens"] = state["live"].get("tokens", 0) + 1
            if payload.get("pass_number") == 3:
                state["p3_buffer"] += payload.get("token", "")
                if "p3_md" in state:
                    state["p3_md"].set_content(f"```\n{state['p3_buffer']}\n```")
                final_md.set_content(f"```\n{state['p3_buffer']}\n```")

        elif name == EventType.AGENT_PASS_RESULT.value:
            n = payload.get("pass_number")
            key = _STEP_TO_NODE.get(n)
            if key:
                strip.set(key, "done")
            state["live"] = {"phase": "", "tokens": 0, "start_ts": None}
            # For Pass 3 we already have a streaming card; replace it with
            # the final card (so it picks up the duration / scores meta).
            if n == 3 and state.get("p3_card_container") is not None:
                state["p3_card_container"].clear()
                state["p3_card_container"].delete()
                state["p3_card_container"] = None
            _add_pass_card(
                pass_number=n,
                pass_name=payload.get("pass_name", f"Pass {n}"),
                content=payload.get("content", ""),
                model=payload.get("model", ""),
                duration_ms=payload.get("duration_ms", 0),
                task_type=payload.get("task_type"),
                technique=payload.get("technique"),
                scores=payload.get("scores"),
            )

        elif name == EventType.PERSONA_RESULT.value:
            strip.set("persona", "done")
            _add_pass_card(
                pass_number=0,
                pass_name="Persona",
                content=payload.get("persona", ""),
                model=payload.get("model", ""),
                duration_ms=payload.get("duration_ms", 0),
            )

        elif name == EventType.MAGNITUDE_START.value:
            state["live"] = {
                "phase": "Magnitude",
                "tokens": 0,
                "start_ts": time.monotonic(),
            }
        elif name == EventType.MAGNITUDE_CHUNK.value:
            state["live"]["tokens"] = state["live"].get("tokens", 0) + 1
            state["magnitude"] += payload.get("token", "")
            mag_md.set_content(state["magnitude"])
        elif name == EventType.MAGNITUDE_DONE.value:
            strip.set("magnitude", "done")
            state["live"] = {"phase": "", "tokens": 0, "start_ts": None}
        elif name == EventType.MAGNITUDE_ERROR.value:
            strip.set("magnitude", "error")
            state["live"] = {"phase": "", "tokens": 0, "start_ts": None}

        elif name == EventType.SOT_START.value:
            state["live"] = {
                "phase": "Skeleton-of-Thought",
                "tokens": 0,
                "start_ts": time.monotonic(),
            }
        elif name == EventType.SOT_CHUNK.value:
            state["live"]["tokens"] = state["live"].get("tokens", 0) + 1
            state["sot"] += payload.get("token", "")
            sot_md.set_content(state["sot"])
        elif name == EventType.SOT_DONE.value:
            strip.set("sot", "done")
            state["live"] = {"phase": "", "tokens": 0, "start_ts": None}
        elif name == EventType.SOT_ERROR.value:
            strip.set("sot", "error")
            state["live"] = {"phase": "", "tokens": 0, "start_ts": None}

        elif name == EventType.ENHANCEMENT_SCORE.value:
            scores = payload.get("scores")
            if scores:
                final_scores_row.clear()
                with final_scores_row:
                    render_score_chips(scores)

        elif name == EventType.AGENT_DONE.value:
            strip.set("done", "done")
            state["live"] = {"phase": "", "tokens": 0, "start_ts": None}

        elif name == EventType.AGENT_DISAMBIGUATE.value:
            # Stash for the post-pipeline resume handler in _run_pipeline_action.
            state["captured_disambig"] = {
                "disambig_id": payload.get("disambig_id"),
                "questions": payload.get("questions") or [],
            }

        elif name == EventType.AGENT_ERROR.value:
            step = payload.get("step", "?")
            ui.notify(f"Error in {step}: {payload.get('error')}", color="negative")
            # Update strip if we know the step
            if step in ("pass1", "pass2", "pass3", "pass4"):
                strip.set(step, "error")

    async def _ask_disambig_modal(questions: list[dict]) -> dict[str, str]:
        """Open a modal with radio groups; resolve to {Q1: answer-text}."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        groups: dict[str, "ui.radio"] = {}

        with ui.dialog(value=True).props("persistent") as dialog, ui.card().classes(
            "min-w-[640px]"
        ):
            ui.label("Pipeline paused — clarify").classes("text-h6 text-white")
            ui.label(
                "Pass 2 found several gaps. Pick an option per question (or "
                "leave blank); the pipeline will resume with your "
                "clarifications injected into Pass 3."
            ).classes("text-caption text-grey")

            for i, q in enumerate(questions):
                qid = f"Q{i + 1}"
                ui.label(f"{qid}: {q['question']}").classes("text-body1 mt-2")
                groups[qid] = ui.radio(
                    options=q.get("options", []),
                ).props("dense").classes("w-full")

            with ui.row().classes("justify-end gap-1 mt-3"):
                def _cancel():
                    if not future.done():
                        future.set_result({})
                    dialog.close()

                def _submit():
                    answers = {
                        qid: r.value for qid, r in groups.items() if r.value
                    }
                    if not future.done():
                        future.set_result(answers)
                    dialog.close()

                ui.button("Skip", on_click=_cancel).props("flat")
                ui.button("Resume pipeline", on_click=_submit).props("color=primary")

        return await future

    async def _run_pipeline_action():
        if state["running"]:
            ui.notify("Already running.", color="warning")
            return
        prompt = (prompt_input.value or "").strip()
        if not prompt:
            ui.notify("Enter a prompt first.", color="warning")
            return

        # Reset UI state
        state["running"] = True
        state["p3_buffer"] = ""
        state["magnitude"] = ""
        state["sot"] = ""
        state["pass_meta"] = {}
        state["pending_disambig"] = {}
        state["captured_disambig"] = {}
        state["log_container"].clear()
        diff_container.clear()
        final_scores_row.clear()
        rr_verdict_container.clear()
        persona_pair_container.clear()
        rr_row.style("display: none;")
        rr_status.set_text("")
        final_md.set_content("_(streaming…)_")
        mag_md.set_content("_(magnitude pending)_" if magnitude_sw.value else "_(off)_")
        sot_md.set_content("_(sot pending)_" if sot_sw.value else "_(off)_")
        strip.reset()

        live_settings = load()
        provider = get_provider(live_settings)
        chosen_model = live_settings.default_model or (
            scorer_select.value or ""
        )
        if not chosen_model:
            mods = await provider.list_models()
            chosen_model = mods[0] if mods else ""
        if not chosen_model:
            ui.notify("No model available.", color="negative")
            state["running"] = False
            return

        # Pull prior session context (if a session is active in the drawer).
        session_ctx = session_context_for(db_path(), drawer.active_id)

        # Branch state: cleared after this run is wired into PipelineOptions.
        branch = state.pop("branch", None) if state.get("branch") else None
        if branch:
            branch_badge.set_text(
                f"Branched from {branch['parent_run_id'][:8]} @ "
                f"Pass {branch['branch_from_pass']}"
            )
            branch_badge.style("color: var(--accent); display: inline;")
        else:
            branch_badge.set_text("")
            branch_badge.style("display: none;")

        # Read the saved Reasoning Panel config; build a live panel if
        # enabled. Failures are SAFE — we toast a warning and fall back
        # to a no-panel run rather than crashing the Studio.
        panel = None
        panel_kwargs: dict[str, Any] = {}
        try:
            panel_cfg = load_panel_config()
            panel = build_reasoning_panel(panel_cfg)
        except Exception as exc:  # noqa: BLE001
            ui.notify(
                f"Reasoning Panel config invalid — running without panel: {exc}",
                color="warning",
            )
            panel = None
            panel_cfg = None
        if panel is not None and panel_cfg is not None:
            panel_kwargs = dict(
                reasoning_panel=panel,
                panel_mode=panel_cfg.mode,
                panel_aggregator=panel_cfg.aggregator,
            )
            panel_badge.set_text(
                f"Panel: {len(panel.slots)} slots, mode={panel_cfg.mode}"
            )
            panel_badge.style("color: var(--accent); display: inline;")
        else:
            panel_badge.set_text("")
            panel_badge.style("display: none;")

        run_btn.disable()
        try:
            opts_kwargs: dict[str, Any] = dict(
                scorer_model=scorer_select.value or None,
                persona_mode=persona_sw.value,
                magnitude_mode=magnitude_sw.value,
                sot_mode=sot_sw.value,
                temperature=temp_slider.value,
                max_tokens_scale=tokens_slider.value,
                session_id=drawer.active_id,
                parent_run_id=(branch or {}).get("parent_run_id"),
                branch_from_pass=(branch or {}).get("branch_from_pass"),
                branch_db_path=db_path() if branch else None,
            )
            # complement_persona may not exist yet on PipelineOptions if
            # Agent A's pipeline change hasn't merged. Pass it only if the
            # dataclass has the field, so we don't crash on TypeError.
            if (
                "complement_persona"
                in getattr(PipelineOptions, "__dataclass_fields__", {})
            ):
                opts_kwargs["complement_persona"] = (
                    persona_sw.value and complement_persona_sw.value
                )
            result = await run_pipeline(
                prompt,
                provider=provider, model=chosen_model,
                opts=PipelineOptions(**opts_kwargs),
                on_event=_on_event,
                session_context=session_ctx,
                request_timeout=live_settings.request_timeout,
                idle_timeout=live_settings.idle_timeout,
                pending_disambig=state["pending_disambig"],
                **panel_kwargs,
            )

            # Disambiguation pause — open modal, resume on user submit.
            captured = state["captured_disambig"]
            if (
                result.extras
                and result.extras.get("paused")
                and captured.get("disambig_id")
            ):
                snapshot = state["pending_disambig"].get(captured["disambig_id"])
                if snapshot is not None:
                    answers = await _ask_disambig_modal(captured["questions"])
                    resume_state = build_resume_state(snapshot, answers)
                    ui.notify("Resuming pipeline with your clarifications…",
                              color="info")
                    result = await run_pipeline(
                        snapshot["prompt"],
                        provider=provider, model=chosen_model,
                        opts=PipelineOptions(
                            scorer_model=snapshot.get("scorer_model"),
                            persona_mode=snapshot.get("persona_mode", False),
                            magnitude_mode=snapshot.get("magnitude_mode", False),
                            sot_mode=snapshot.get("sot_mode", False),
                            session_id=snapshot.get("session_id"),
                            temperature=temp_slider.value,
                            max_tokens_scale=tokens_slider.value,
                            resume_state=resume_state,
                        ),
                        on_event=_on_event,
                        session_context=session_ctx,
                        request_timeout=live_settings.request_timeout,
                        idle_timeout=live_settings.idle_timeout,
                    )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Pipeline failed: {exc}", color="negative")
            state["running"] = False
            run_btn.enable()
            return

        state["last_result"] = result
        record = result.extras.get("_record") if result.extras else None
        if record is not None:
            runs_module.save(record, db_path(), jsonl_log_path())
            drawer.refresh()  # entry-count for active session may have bumped
            # Track the most recent run so the ↗ buttons on its pass cards
            # know which parent to fork from.
            state["last_run_id"] = record.id
        elif result.run_id:
            state["last_run_id"] = result.run_id

        final_md.set_content(f"```\n{result.result}\n```")
        with diff_container:
            render_diff(prompt, result.result)
        # Render Persona A / Persona B side-by-side if either is present.
        _render_persona_pair(
            getattr(result, "persona", None) or "",
            getattr(result, "persona_partner", None) or "",
        )
        # Reveal the Round Robin handoff button now that we have a result.
        rr_row.style("display: flex;")
        run_btn.enable()
        state["running"] = False
        ui.notify(
            f"Done — improvement {result.scores.get('improvement')}% "
            f"({chosen_model})",
            color="positive",
        )

        # Auto-fire the persona handoff to round-robin when the user
        # opted in by ticking "Generate partner persona" AND we have a
        # populated Persona B. The manual button stays as a re-fire path
        # for cases where round-robin was offline the first time.
        # Silent on skip: if Persona A or B is empty for any reason,
        # bail without surfacing a "nothing to send" toast — the user
        # didn't explicitly click anything.
        try:
            if (
                complement_persona_sw.value
                and getattr(result, "persona", None)
                and getattr(result, "persona_partner", None)
            ):
                await _send_personas_to_round_robin(silent_on_skip=True)
        except Exception as exc:  # noqa: BLE001 — never fail run-complete
            ui.notify(
                f"Auto-handoff failed: {exc}", color="negative"
            )

    run_btn.on_click(_run_pipeline_action)

    # populate scorer dropdown on first load
    ui.timer(0.5, _refresh_models_async, once=True)
