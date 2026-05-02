"""Session drawer — left-side overlay showing prior runs in the active session.

Lets the user create, switch, rename, and delete sessions, and shows a
recency-ordered list of runs persisted with each session id. The session
context is what `core.pipeline.run_pipeline` consumes via
``opts.session_id`` — the drawer also exposes the active id for the
Studio page to pass through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from nicegui import ui

from ...persistence import sessions as sessions_module
from ...persistence.db import connect


class SessionDrawer:
    """Stateful drawer; owns the active session id and a change-listener hook."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._active_id: str | None = None
        self._on_change: Callable[[str | None], None] | None = None
        self._list_container: ui.element | None = None

    @property
    def active_id(self) -> str | None:
        return self._active_id

    def on_change(self, fn: Callable[[str | None], None]) -> None:
        self._on_change = fn

    def render(self) -> None:
        with ui.right_drawer(value=False, fixed=True).classes(
            "bg-[var(--bg-2)] p-3"
        ).props("bordered") as drawer:
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Sessions").classes("text-h6 text-white")
                ui.button(icon="close", on_click=drawer.toggle).props("flat dense")

            with ui.row().classes("gap-1 mt-1 w-full"):
                name_input = ui.input(placeholder="New session name").props(
                    "dense outlined"
                ).classes("flex-1")
                ui.button("New", icon="add", on_click=lambda: self._create(name_input))

            ui.button("Clear active", icon="logout",
                      on_click=lambda: self._set_active(None)).props("flat dense")
            ui.separator().classes("my-2")
            self._list_container = ui.column().classes("w-full gap-1")
        self.refresh()

    # ── public ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._list_container is None:
            return
        self._list_container.clear()
        with self._list_container:
            sessions = sessions_module.list_all(self._db_path, active_id=self._active_id)
            if not sessions:
                ui.label("No sessions yet — create one above.").classes(
                    "text-caption text-grey"
                )
                return
            for s in sessions:
                self._render_row(s)

    # ── internals ───────────────────────────────────────────────────

    def _render_row(self, s) -> None:
        is_active = s.id == self._active_id
        bg = "var(--accent)" if is_active else "var(--bg-3)"
        with ui.row().classes("items-center w-full gap-1").style(
            f"background: {bg}; padding: 4px 6px; border-radius: 6px;"
        ):
            with ui.column().classes("flex-1 gap-0"):
                ui.label(s.name).classes(
                    "text-body2 " + ("text-white" if is_active else "")
                )
                ui.label(f"{s.entry_count} runs · {s.updated_at[:16]}").classes(
                    "text-caption text-grey"
                )
            ui.button(
                icon="play_arrow",
                on_click=lambda sid=s.id: self._set_active(sid),
            ).props("flat dense").tooltip("Activate")
            ui.button(
                icon="edit",
                on_click=lambda sid=s.id, name=s.name: self._rename(sid, name),
            ).props("flat dense").tooltip("Rename")
            ui.button(
                icon="delete",
                on_click=lambda sid=s.id: self._delete(sid),
            ).props("flat dense").tooltip("Delete")

    def _create(self, name_input) -> None:
        name = (name_input.value or "").strip()
        sessions_module.create(self._db_path, name)
        name_input.set_value("")
        self.refresh()

    def _set_active(self, sid: str | None) -> None:
        self._active_id = sid
        if self._on_change:
            self._on_change(sid)
        if sid is not None:
            sessions_module.touch(self._db_path, sid)
        self.refresh()

    def _rename(self, sid: str, current_name: str) -> None:
        with ui.dialog() as dialog, ui.card():
            new_name = ui.input("New name", value=current_name).props(
                "dense outlined autofocus"
            )
            with ui.row().classes("justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                def _do_rename():
                    if sessions_module.rename(self._db_path, sid, new_name.value):
                        ui.notify("Renamed.", color="positive")
                    dialog.close()
                    self.refresh()
                ui.button("Rename", on_click=_do_rename).props("color=primary")
        dialog.open()

    def _delete(self, sid: str) -> None:
        with ui.dialog() as dialog, ui.card():
            ui.label("Delete this session? Runs are kept; only the session "
                    "grouping is removed.").classes("text-body2")
            with ui.row().classes("justify-end gap-1"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                def _confirm():
                    sessions_module.delete(self._db_path, sid)
                    if self._active_id == sid:
                        self._set_active(None)
                    dialog.close()
                    self.refresh()
                ui.button("Delete", on_click=_confirm).props("color=negative")
        dialog.open()


def session_context_for(db_path: Path, sid: str | None) -> str:
    """Helper for the Studio: build the session_context string for run_pipeline."""
    if not sid:
        return ""
    session = sessions_module.get(db_path, sid)
    if not session:
        return ""
    return sessions_module.build_context(session)
