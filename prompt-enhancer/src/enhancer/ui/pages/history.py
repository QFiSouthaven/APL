"""History page — browse, filter, and inspect past runs.

Click any row to open a detail panel with:
* the original / enhanced prompts (diff view),
* the four score chips,
* the branch tree (parents + children of this run).
"""

from __future__ import annotations

from nicegui import ui

from ...config import db_path
from ...persistence import runs as runs_module
from ..components.branch_tree import render_branch_tree
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

    with ui.row().classes("w-full items-center p-3 gap-2"):
        ui.label("History").classes("text-h5 text-white")
        task_filter = ui.input("task type filter").props("dense outlined")
        min_imp = ui.number("min improvement %", value=0, min=0, max=100).props(
            "dense outlined"
        )
        limit = ui.number("limit", value=50, min=1, max=500).props("dense outlined")
        ui.button(
            "Refresh", icon="refresh",
            on_click=lambda: _reload(table, task_filter, min_imp, limit),
        )

    columns = [
        {"name": "ts", "label": "When", "field": "ts", "sortable": True},
        {"name": "task_type", "label": "Task", "field": "task_type"},
        {"name": "improvement", "label": "Improve %", "field": "improvement",
         "sortable": True},
        {"name": "model", "label": "Model", "field": "model"},
        {"name": "prompt", "label": "Prompt", "field": "prompt"},
        {"name": "id", "label": "Run id", "field": "id"},
    ]
    rows = _fetch_rows()

    detail = ui.column().classes("w-full p-3 gap-2 mt-2")  # filled on row click

    table = ui.table(
        columns=columns, rows=rows, row_key="id", pagination=20,
    ).classes("w-full")
    table.props("dense flat dark selection=single")

    table.on(
        "rowClick",
        lambda e: _show_detail(detail, _row_id(e)),
    )


def _row_id(event) -> str | None:
    """Extract the run id from a NiceGUI rowClick event payload."""
    args = getattr(event, "args", None)
    # NiceGUI delivers [evt, row, index]; row is the rendered row dict.
    if isinstance(args, list) and len(args) >= 2 and isinstance(args[1], dict):
        return args[1].get("id")
    return None


def _show_detail(container: ui.element, run_id: str | None) -> None:
    container.clear()
    if not run_id:
        return
    record = runs_module.get_run(db_path(), run_id)
    if not record:
        with container:
            ui.label(f"Run {run_id} not found.").classes("text-grey")
        return

    with container:
        with ui.element("div").classes("studio-card"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"Run {record['id']}").classes("text-h6 text-white")
                ui.label(record["ts"]).classes("text-caption text-grey")
            scores = {
                k: record.get(k) for k in
                ("specificity", "constraints", "actionability", "improvement")
                if record.get(k) is not None
            }
            if scores:
                render_score_chips(scores)
            ui.label(f"task: {record.get('task_type') or '—'}  ·  "
                     f"technique: {record.get('technique') or '—'}  ·  "
                     f"model: {record.get('model') or '—'}").classes(
                "text-caption text-grey"
            )

        with ui.expansion("Original ↔ Enhanced diff", icon="compare",
                          value=True).classes("w-full"):
            render_diff(record["prompt"], record["enhanced_prompt"])

        with ui.element("div").classes("studio-card"):
            ui.label("Branching").classes("text-caption text-grey")
            render_branch_tree(db_path(), run_id)


def _fetch_rows(task_type=None, min_improvement=None, limit=50) -> list[dict]:
    raw = runs_module.list_recent(
        db_path(), limit=limit,
        task_type=task_type if task_type else None,
        min_improvement=min_improvement if min_improvement and min_improvement > 0 else None,
    )
    return [
        {
            "ts": (r["ts"] or "")[:19].replace("T", " "),
            "task_type": r.get("task_type") or "—",
            "improvement": r.get("improvement"),
            "model": (r.get("model") or "—")[:30],
            "prompt": (r.get("prompt") or "")[:80],
            "id": r["id"],
        }
        for r in raw
    ]


def _reload(table, task_filter, min_imp, limit) -> None:
    table.rows = _fetch_rows(
        task_type=(task_filter.value or "").strip() or None,
        min_improvement=int(min_imp.value or 0),
        limit=int(limit.value or 50),
    )
    table.update()
