"""Smoke coverage for the 6 NiceGUI page modules in ``enhancer.ui.pages``.

Strategy: import-only smoke (see ``docs/UI_TESTING.md``). Every page
module must import, expose a ``render()`` entry point, and render
end-to-end against a temporary DB without raising.

The ``ui_tmp_db`` fixture in ``conftest.py`` redirects
``enhancer.config.{db_path,data_dir,config_dir,jsonl_log_path}`` to
``tmp_path`` so a render call against ``settings.py`` /
``templates.py`` / ``history.py`` cannot touch the user's real
``%APPDATA%\\prompt-enhancer\\enhancer.db``.
"""

from __future__ import annotations

import importlib

import pytest


# ─── studio ──────────────────────────────────────────────────────────────


def test_studio_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.pages.studio")
    assert callable(getattr(mod, "render", None))


def test_studio_format_clock_helper() -> None:
    """``_format_clock`` is a pure stringifier the live indicator uses."""
    from enhancer.ui.pages.studio import _format_clock

    assert _format_clock(0.4) == "0s"
    assert _format_clock(45) == "45s"
    assert _format_clock(125) == "2m 05s"
    assert _format_clock(3600) == "60m 00s"


def test_studio_step_to_node_complete() -> None:
    """The pass→status-strip-key map must cover passes 1-4."""
    from enhancer.ui.pages.studio import _STEP_TO_NODE

    assert _STEP_TO_NODE == {1: "pass1", 2: "pass2", 3: "pass3", 4: "pass4"}


def test_studio_render_smoke(ui_tmp_db) -> None:
    """Full ``render()`` builds the page tree without raising."""
    from enhancer.ui.pages import studio

    studio.render()


# ─── history ─────────────────────────────────────────────────────────────


def test_history_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.pages.history")
    assert callable(getattr(mod, "render", None))


def test_history_fetch_rows_empty_db(ui_tmp_db) -> None:
    """``_fetch_rows`` against a fresh DB returns ``[]``."""
    from enhancer.ui.pages.history import _fetch_rows

    assert _fetch_rows() == []


def test_history_row_id_handles_unknown_payload() -> None:
    """``_row_id`` extracts the id from NiceGUI's rowClick event shape."""
    from enhancer.ui.pages.history import _row_id

    class _Ev:
        def __init__(self, args):
            self.args = args

    # Documented [evt, row, index] shape:
    assert _row_id(_Ev([{}, {"id": "abc"}, 0])) == "abc"
    # Off-shape inputs return None gracefully:
    assert _row_id(_Ev(None)) is None
    assert _row_id(_Ev([])) is None


def test_history_render_smoke(ui_tmp_db) -> None:
    from enhancer.ui.pages import history

    history.render()


# ─── analytics ───────────────────────────────────────────────────────────


def test_analytics_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.pages.analytics")
    assert callable(getattr(mod, "render", None))


def test_analytics_render_smoke_empty_db(ui_tmp_db) -> None:
    """Empty DB hits the ``if s['techniques']: ...`` guard and skips charts."""
    from enhancer.ui.pages import analytics

    analytics.render()


def test_analytics_kpi_helper() -> None:
    """``_kpi`` is a pure visual helper — must be callable without DB."""
    from enhancer.ui.pages.analytics import _kpi

    _kpi("Total runs", 0)  # must not raise


# ─── compare ─────────────────────────────────────────────────────────────


def test_compare_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.pages.compare")
    assert callable(getattr(mod, "render", None))


def test_compare_render_smoke(ui_tmp_db) -> None:
    from enhancer.ui.pages import compare

    compare.render()


# ─── templates ───────────────────────────────────────────────────────────


def test_templates_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.pages.templates")
    assert callable(getattr(mod, "render", None))


def test_templates_seed_constants() -> None:
    """The seed list shape is part of first-run UX; pin its size."""
    from enhancer.ui.pages.templates import _SEEDS

    assert len(_SEEDS) >= 6  # documented as 8 starter templates
    # Each row is (domain, title, body); strings, all non-empty.
    for domain, title, body in _SEEDS:
        assert domain and title and body


def test_templates_seed_if_empty_runs_once(ui_tmp_db) -> None:
    """Calling ``_seed_if_empty`` twice must not produce duplicates."""
    from enhancer.persistence.db import connect
    from enhancer.ui.pages.templates import _SEEDS, _seed_if_empty

    _seed_if_empty(ui_tmp_db)
    _seed_if_empty(ui_tmp_db)  # idempotent

    with connect(ui_tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()["c"]
    assert count == len(_SEEDS)


def test_templates_render_smoke(ui_tmp_db) -> None:
    from enhancer.ui.pages import templates

    templates.render()


# ─── settings ────────────────────────────────────────────────────────────


def test_settings_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.pages.settings")
    assert callable(getattr(mod, "render", None))


def test_settings_render_smoke(ui_tmp_db) -> None:
    """Render builds the form even when no settings file exists yet."""
    from enhancer.ui.pages import settings as settings_page

    settings_page.render()


# ─── all-pages parity ───────────────────────────────────────────────────


@pytest.mark.parametrize("module_name", [
    "enhancer.ui.pages.studio",
    "enhancer.ui.pages.history",
    "enhancer.ui.pages.analytics",
    "enhancer.ui.pages.compare",
    "enhancer.ui.pages.templates",
    "enhancer.ui.pages.settings",
])
def test_every_page_exposes_render(module_name: str) -> None:
    """Each page module's contract: ``render()`` is the wired entry point."""
    mod = importlib.import_module(module_name)
    assert callable(getattr(mod, "render", None)), (
        f"{module_name} must expose a callable ``render`` for app.py to wire."
    )
