"""Per-pass result card — used in the Studio Log tab.

Shows the pass name, model, duration, optional task_type / technique /
scores chips, and the streamed content with a copy button. Replaces the
plain `ui.label` log entries the early Studio scaffold used.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from .score_chips import render_score_chips


def render_pass_card(
    *,
    pass_number: int,
    pass_name: str,
    content: str,
    model: str = "",
    duration_ms: int = 0,
    task_type: str | None = None,
    technique: str | None = None,
    scores: dict[str, int] | None = None,
    error: str | None = None,
) -> ui.element:
    """Build a card and return its outer element so the caller can update it.

    Used both for finished passes (full content) and live-streaming
    passes (caller updates the inner content via the returned element's
    children — see Studio's Pass 3 streaming flow).
    """
    border = "var(--bad)" if error else "var(--border)"
    card = ui.element("div").classes("studio-card").style(
        f"border-left: 3px solid {border};"
    )
    with card:
        # Header row: pass number + name + meta
        with ui.row().classes("items-center justify-between gap-3 w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.element("span").classes("score-chip").style(
                    "background: var(--accent); color: white;"
                ).props(f'data-pass={pass_number}').tooltip(f"Pass {pass_number}")
                ui.label(f"{pass_name}").classes("text-body1 text-white")
                if task_type:
                    ui.element("span").classes("score-chip").tooltip("task type")
                    ui.label(task_type).classes("text-caption")
                if technique:
                    ui.element("span").classes("score-chip").tooltip("PRIMARY FOCUS")
                    ui.label(technique).classes("text-caption")
            with ui.row().classes("items-center gap-2"):
                if duration_ms:
                    ui.label(_fmt_duration(duration_ms)).classes("text-caption text-grey")
                if model:
                    ui.label(_truncate_model(model)).classes("text-caption text-grey")

        if scores:
            render_score_chips(scores)

        if error:
            ui.label(f"⚠ {error}").classes("text-caption").style("color: var(--bad);")
        else:
            ui.markdown(f"```\n{content or ''}\n```")

        with ui.row().classes("justify-end"):
            ui.button(
                icon="content_copy", on_click=lambda c=content: _copy(c),
            ).props("flat dense").tooltip("Copy")
    return card


def _fmt_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s"


def _truncate_model(name: str, n: int = 30) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _copy(text: str) -> None:
    try:
        import pyperclip
        pyperclip.copy(text)
        ui.notify("Copied to clipboard.", color="positive")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Copy failed: {exc}", color="negative")
