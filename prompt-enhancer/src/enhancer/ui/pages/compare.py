"""Compare page — A/B run the same prompt across two scorers, side-by-side.

**Always serial** — never parallel. Single-instance LM Studio / LM Link
backends queue concurrent calls and time out (the very lesson the source
monolith taught us; see ``docs/EXTRACTION_GOTCHAS.md`` §3).
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from ...config import db_path, jsonl_log_path, load
from ...core.events import EventType
from ...core.pipeline import PipelineOptions, run_pipeline
from ...llm.registry import get_provider
from ...persistence import runs as runs_module
from ..components.diff_view import render_diff
from ..components.score_chips import render_score_chips


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


def render() -> None:
    _sidebar()
    settings = load()

    with ui.row().classes("w-full items-center p-3 gap-2"):
        ui.label("Compare").classes("text-h5 text-white")

    with ui.column().classes("p-3 gap-3 max-w-[1200px]"):
        with ui.element("div").classes("studio-card"):
            ui.label(
                "Run the same prompt twice — once with each model — to "
                "see the score delta. Pipelines run SERIALLY (single-"
                "instance LM Studio / LM Link backends queue concurrent "
                "calls and time out)."
            ).classes("text-caption text-grey")

        with ui.row().classes("gap-2 w-full"):
            model_a_select = ui.select([], label="Model A").classes("flex-1")
            model_b_select = ui.select([], label="Model B").classes("flex-1")
            ui.button(icon="refresh",
                      on_click=lambda: _refresh_models(model_a_select, model_b_select))

        prompt_input = ui.textarea(
            label="Prompt",
            placeholder="Compare the two models on this prompt…",
        ).classes("w-full").props("rows=3 dense outlined")

        with ui.row().classes("gap-2 items-center"):
            temp_slider = ui.slider(
                min=0.0, max=2.0, step=0.05, value=settings.temperature,
            ).classes("flex-1")
            ui.label(f"temperature = {settings.temperature}").bind_text_from(
                temp_slider, "value", backward=lambda v: f"temperature = {v:.2f}"
            )

        run_btn = ui.button("Run compare", icon="compare_arrows").props(
            "color=primary"
        )
        status_label = ui.label("").classes("text-caption text-grey")

        with ui.row().classes("gap-3 w-full items-stretch"):
            col_a = ui.column().classes("flex-1 gap-2")
            col_b = ui.column().classes("flex-1 gap-2")

        diff_section = ui.column().classes("w-full mt-3 gap-2")

    state: dict[str, Any] = {"running": False}

    async def _refresh_models_async() -> None:
        provider = get_provider(load())
        try:
            models = await provider.list_models()
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"list_models failed: {exc}", color="negative")
            return
        for sel in (model_a_select, model_b_select):
            sel.options = models
            if not sel.value and models:
                sel.value = models[0]
            sel.update()

    def _refresh_models(_a, _b) -> None:
        ui.timer(0.01, _refresh_models_async, once=True)

    def _render_side(col: ui.element, label: str, model: str, result, prompt: str) -> None:
        col.clear()
        with col:
            with ui.element("div").classes("studio-card"):
                ui.label(f"{label}  ·  {model}").classes("text-body1 text-white")
                if result is None:
                    ui.label("—").classes("text-caption text-grey")
                    return
                if result.scores:
                    render_score_chips(result.scores)
                ui.markdown(f"```\n{result.result}\n```")

    async def _run() -> None:
        if state["running"]:
            ui.notify("Already running.", color="warning")
            return
        prompt = (prompt_input.value or "").strip()
        if not prompt:
            ui.notify("Enter a prompt.", color="warning")
            return
        m_a = model_a_select.value
        m_b = model_b_select.value
        if not m_a or not m_b:
            ui.notify("Pick both models.", color="warning")
            return

        state["running"] = True
        run_btn.disable()
        col_a.clear(); col_b.clear(); diff_section.clear()
        status_label.set_text("Running model A…")

        live_settings = load()
        provider = get_provider(live_settings)

        async def _on_event(*_, **__):
            return  # quiet — UI updates only on completion

        try:
            result_a = await run_pipeline(
                prompt, provider=provider, model=m_a,
                opts=PipelineOptions(temperature=temp_slider.value),
                on_event=_on_event,
                request_timeout=live_settings.request_timeout,
                idle_timeout=live_settings.idle_timeout,
            )
            _render_side(col_a, "A", m_a, result_a, prompt)

            status_label.set_text("Running model B…")
            result_b = await run_pipeline(
                prompt, provider=provider, model=m_b,
                opts=PipelineOptions(temperature=temp_slider.value),
                on_event=_on_event,
                request_timeout=live_settings.request_timeout,
                idle_timeout=live_settings.idle_timeout,
            )
            _render_side(col_b, "B", m_b, result_b, prompt)

            # Persist both for history.
            for r in (result_a, result_b):
                rec = r.extras.get("_record") if r.extras else None
                if rec:
                    runs_module.save(rec, db_path(), jsonl_log_path())

            # Score-delta summary
            with diff_section:
                with ui.element("div").classes("studio-card"):
                    ui.label("Score delta (B − A)").classes("text-caption text-grey")
                    delta = {
                        k: (result_b.scores.get(k, 0) - result_a.scores.get(k, 0))
                        for k in ("specificity", "constraints",
                                  "actionability", "improvement")
                    }
                    with ui.row().classes("gap-3"):
                        for k, v in delta.items():
                            color = "var(--good)" if v > 0 else "var(--bad)" if v < 0 else "var(--text-2)"
                            with ui.element("div").classes("score-chip").style(
                                f"color: {color}; border-color: {color};"
                            ):
                                ui.label(f"{k}: {v:+}").classes("text-caption")
                with ui.element("div").classes("studio-card"):
                    ui.label("A → B diff").classes("text-caption text-grey")
                    render_diff(result_a.result, result_b.result)

            status_label.set_text(
                f"Done — A: +{result_a.scores.get('improvement')}%  ·  "
                f"B: +{result_b.scores.get('improvement')}%"
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Compare failed: {exc}", color="negative")
        finally:
            state["running"] = False
            run_btn.enable()

    run_btn.on_click(_run)
    ui.timer(0.5, _refresh_models_async, once=True)
