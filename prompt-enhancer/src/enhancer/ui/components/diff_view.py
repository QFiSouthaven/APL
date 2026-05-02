"""Side-by-side diff view of original vs enhanced prompt."""

from __future__ import annotations

import difflib

from nicegui import ui


def render_diff(original: str, enhanced: str) -> None:
    """Render an HTML side-by-side diff inside an iframe-friendly container.

    Uses ``difflib.HtmlDiff`` (stdlib) so no extra dependency is needed.
    """
    if not original and not enhanced:
        ui.label("No content to diff.").classes("text-grey")
        return
    differ = difflib.HtmlDiff(wrapcolumn=70)
    html = differ.make_table(
        original.splitlines() or [""],
        enhanced.splitlines() or [""],
        fromdesc="Original",
        todesc="Enhanced",
        context=False,
    )
    # Inject minimal dark-theme overrides so the stdlib HTML reads on dark.
    html = (
        "<style>"
        "table.diff{font-family:Consolas,Menlo,monospace;font-size:12px;"
        "background:#0f1115;color:#e6e9ef;border-collapse:collapse;width:100%;}"
        "td.diff_header{background:#1f2530;color:#9aa3b2;padding:2px 6px;}"
        ".diff_next{background:#1f2530;}"
        ".diff_add{background:#1c3320;color:#9be4b4;}"
        ".diff_chg{background:#33260f;color:#ffd58a;}"
        ".diff_sub{background:#3a1818;color:#ff8e94;}"
        "</style>"
        + html
    )
    ui.html(html)
