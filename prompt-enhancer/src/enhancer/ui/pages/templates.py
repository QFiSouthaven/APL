"""Templates page — CRUD over the ``templates`` table.

On first visit, seeds 8 starter templates spanning common domains so
the user has something to fork from. Click any template to copy its
body into the Studio prompt input via clipboard + redirect.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from nicegui import ui

from ...config import db_path
from ...persistence.db import connect


_SEEDS: list[tuple[str, str, str]] = [
    (
        "coding", "API spec generator",
        "Given a function signature in PYTHON, produce: 1) a one-line "
        "purpose summary, 2) parameter descriptions with types, 3) "
        "return-value description, 4) at least 2 doctest examples, 5) "
        "edge cases the implementation must handle. Output as a "
        "Markdown docstring block.",
    ),
    (
        "creative", "Short fiction prompt",
        "Write a 500-word literary short story with: a non-omniscient "
        "narrator, a protagonist whose internal goal contradicts their "
        "external goal, a recurring sensory motif, and an ambiguous "
        "ending. Output the story only.",
    ),
    (
        "analytical", "Research analysis brief",
        "Produce an analytical brief on the following claim. Structure: "
        "1) Claim restated precisely, 2) Strongest supporting evidence, "
        "3) Strongest counter-evidence, 4) Three falsification tests, "
        "5) Confidence level (low/medium/high) with one-sentence "
        "justification. No filler.",
    ),
    (
        "instructional", "How-to with checks",
        "Write a 5-step how-to for the task below. After each step add "
        "a 'Verify:' line specifying exactly what the user should "
        "observe to confirm success. Use imperative voice.",
    ),
    (
        "conversational", "Empathy-first reply",
        "Reply to the message below in 3-5 sentences. Begin by "
        "reflecting the writer's emotional state in your own words, "
        "then offer one practical, non-prescriptive suggestion. Avoid "
        "platitudes and avoid telling them how to feel.",
    ),
    (
        "factual", "Sourced summary",
        "Summarize the topic in 200-300 words. Use plain language. "
        "Each non-trivial claim must be tagged with a [#] inline citation "
        "marker; list the sources at the end. If you cannot source a "
        "claim, omit it.",
    ),
    (
        "analytical", "Pre-mortem critique",
        "Imagine the project below has failed in 12 months. Describe "
        "the most plausible failure mode in 4 sentences, then list 3 "
        "leading indicators that would surface it early, then 3 "
        "concrete preventions ranked by ROI. No padding.",
    ),
    (
        "creative", "Persona letter",
        "Write a 250-word letter from the persona below to a person "
        "they have never met. The letter must reveal the persona's "
        "voice through specific sensory details and one well-chosen "
        "anachronism. Sign off in character.",
    ),
]


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


def _seed_if_empty(db: Path) -> None:
    with connect(db) as conn:
        c = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()["c"]
        if c > 0:
            return
        with conn:
            for domain, title, body in _SEEDS:
                conn.execute(
                    "INSERT INTO templates (id, domain, title, body, "
                    "created_at, source) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        secrets.token_hex(8), domain, title, body,
                        datetime.now().isoformat(), "seed",
                    ),
                )


def render() -> None:
    _sidebar()
    db = db_path()
    _seed_if_empty(db)

    with ui.row().classes("w-full items-center p-3 gap-2 justify-between"):
        ui.label("Templates").classes("text-h5 text-white")
        ui.button("New template", icon="add",
                  on_click=lambda: _open_editor(None, db, list_container))

    list_container = ui.column().classes("p-3 gap-3 max-w-[900px]")
    _refresh(list_container, db)


def _refresh(container: ui.element, db: Path) -> None:
    container.clear()
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT id, domain, title, body, created_at, source "
            "FROM templates ORDER BY domain, title"
        ).fetchall()
    if not rows:
        with container:
            ui.label("No templates yet.").classes("text-caption text-grey")
        return
    with container:
        for r in rows:
            _render_row(r, db, container)


def _render_row(r, db: Path, container: ui.element) -> None:
    with ui.element("div").classes("studio-card"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-0 flex-1"):
                ui.label(r["title"]).classes("text-body1 text-white")
                ui.label(f"{r['domain']}  ·  {r['source'] or 'user'}").classes(
                    "text-caption text-grey"
                )
            with ui.row().classes("gap-1"):
                ui.button(icon="content_copy",
                          on_click=lambda body=r["body"]: _copy(body)).props(
                    "flat dense").tooltip("Copy body")
                ui.button(icon="edit",
                          on_click=lambda rid=r["id"]: _open_editor(rid, db, container)).props(
                    "flat dense").tooltip("Edit")
                ui.button(icon="delete",
                          on_click=lambda rid=r["id"]: _delete(rid, db, container)).props(
                    "flat dense").tooltip("Delete")
        ui.markdown(f"```\n{r['body']}\n```")


def _copy(body: str) -> None:
    try:
        import pyperclip
        pyperclip.copy(body)
        ui.notify("Template body copied — paste into Studio.", color="positive")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Copy failed: {exc}", color="negative")


def _delete(rid: str, db: Path, container: ui.element) -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label("Delete this template?").classes("text-body2")
        with ui.row().classes("justify-end gap-1"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            def _confirm():
                with connect(db) as conn:
                    with conn:
                        conn.execute("DELETE FROM templates WHERE id = ?", (rid,))
                dialog.close()
                _refresh(container, db)
            ui.button("Delete", on_click=_confirm).props("color=negative")
    dialog.open()


def _open_editor(rid: str | None, db: Path, container: ui.element) -> None:
    existing = None
    if rid:
        with connect(db) as conn:
            existing = conn.execute(
                "SELECT id, domain, title, body FROM templates WHERE id = ?",
                (rid,),
            ).fetchone()

    with ui.dialog() as dialog, ui.card().classes("min-w-[600px]"):
        ui.label("Edit template" if existing else "New template").classes(
            "text-h6 text-white"
        )
        domain_input = ui.input(
            "domain", value=existing["domain"] if existing else "general",
        ).props("dense outlined").classes("w-full")
        title_input = ui.input(
            "title", value=existing["title"] if existing else "",
        ).props("dense outlined").classes("w-full")
        body_input = ui.textarea(
            label="body",
            value=existing["body"] if existing else "",
        ).props("rows=8 dense outlined").classes("w-full")

        with ui.row().classes("justify-end gap-1"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            def _save():
                title = (title_input.value or "").strip()
                body = (body_input.value or "").strip()
                domain = (domain_input.value or "general").strip()
                if not title or not body:
                    ui.notify("Title and body are required.", color="warning")
                    return
                with connect(db) as conn:
                    with conn:
                        if existing:
                            conn.execute(
                                "UPDATE templates SET domain=?, title=?, body=? "
                                "WHERE id=?",
                                (domain, title, body, existing["id"]),
                            )
                        else:
                            conn.execute(
                                "INSERT INTO templates (id, domain, title, body, "
                                "created_at, source) VALUES (?, ?, ?, ?, ?, 'user')",
                                (
                                    secrets.token_hex(8), domain, title, body,
                                    datetime.now().isoformat(),
                                ),
                            )
                dialog.close()
                _refresh(container, db)
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()
