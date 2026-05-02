"""Analytics page — aggregate counters + simple charts."""

from __future__ import annotations

from nicegui import ui

from ...config import db_path
from ...persistence import runs as runs_module


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

    with ui.column().classes("p-4 gap-3 w-full"):
        ui.label("Analytics").classes("text-h5 text-white")
        s = runs_module.stats(db_path())

        with ui.row().classes("gap-3 w-full"):
            _kpi("Total runs", s["total_runs"])
            avg = s.get("average_scores", {}) or {}
            _kpi("Avg improvement",
                 f"{int(avg.get('improvement') or 0)}%" if avg.get("improvement") else "—")
            _kpi("Avg specificity",
                 f"{round(avg.get('specificity') or 0, 1)}" if avg.get("specificity") else "—")
            _kpi("Avg actionability",
                 f"{round(avg.get('actionability') or 0, 1)}" if avg.get("actionability") else "—")
            _kpi("Last run", (s.get("last_ts") or "—")[:19].replace("T", " "))

        if s["techniques"]:
            with ui.element("div").classes("studio-card w-full"):
                ui.label("Technique distribution").classes("text-caption text-grey")
                ui.echart({
                    "tooltip": {"trigger": "item"},
                    "series": [{
                        "type": "pie", "radius": ["40%", "70%"],
                        "data": [
                            {"name": k, "value": v}
                            for k, v in s["techniques"].items()
                        ],
                    }],
                })
        if s["task_types"]:
            with ui.element("div").classes("studio-card w-full"):
                ui.label("Task-type distribution").classes("text-caption text-grey")
                ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "category", "data": list(s["task_types"].keys())},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": list(s["task_types"].values())}],
                })


def _kpi(label: str, value) -> None:
    with ui.element("div").classes("studio-card flex-1"):
        ui.label(label).classes("text-caption text-grey")
        ui.label(str(value)).classes("text-h4 text-white")
