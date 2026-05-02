"""Score chip row — specificity / constraints / actionability / improvement.

Each chip is colored by a band: red <4, amber 4-6, green ≥7.
``improvement`` is on a 0-100 scale with bands red <30, amber 30-60, green ≥60.
"""

from __future__ import annotations

from nicegui import ui


_BAND_COLORS = {
    "red":   "color: var(--bad); border-color: var(--bad);",
    "amber": "color: var(--warn); border-color: var(--warn);",
    "green": "color: var(--good); border-color: var(--good);",
    "grey":  "color: var(--text-2);",
}


def _band_for(value: int | None, *, scale10: bool) -> str:
    if value is None:
        return "grey"
    if scale10:
        if value < 4:
            return "red"
        if value < 7:
            return "amber"
        return "green"
    # 0-100 scale (improvement %)
    if value < 30:
        return "red"
    if value < 60:
        return "amber"
    return "green"


def render_score_chips(scores: dict[str, int]) -> None:
    """Render the four standard chips inline. Safe with missing keys."""
    with ui.row().classes("gap-1 mt-1 mb-1"):
        for label, key, scale10 in (
            ("S",  "specificity",   True),
            ("C",  "constraints",   True),
            ("A",  "actionability", True),
            ("Δ%", "improvement",   False),
        ):
            v = scores.get(key)
            band = _band_for(v, scale10=scale10)
            text = f"{label} {v}" if v is not None else f"{label} —"
            chip = ui.element("span").classes("score-chip").style(_BAND_COLORS[band])
            with chip:
                ui.label(text).classes("text-caption")
            chip.tooltip(key)
