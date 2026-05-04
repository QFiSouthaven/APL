"""Smoke coverage for the 6 NiceGUI components in ``enhancer.ui.components``.

Strategy: import-only smoke (see ``docs/UI_TESTING.md``). Each module
must import without raising, expose its documented entry point, and
have its pure helpers behave on at least one representative input.

NiceGUI lets us call render functions outside ``@ui.page`` thanks to its
auto-slot fallback; we exploit that to drive a real call instead of
just asserting callability. Where a component depends on the DB layer
(``branch_tree``, ``session_drawer``), the ``ui_tmp_db`` fixture from
``conftest.py`` redirects the DB path to ``tmp_path`` so we never
pollute the user's real data directory.
"""

from __future__ import annotations

import importlib

import pytest


# ─── status_strip ────────────────────────────────────────────────────────


def test_status_strip_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.components.status_strip")
    assert hasattr(mod, "StatusStrip")
    assert hasattr(mod, "NODE_LABELS")


def test_status_strip_node_labels_complete() -> None:
    """The 9-node strip must include all pipeline phases the Studio drives."""
    from enhancer.ui.components.status_strip import NODE_LABELS

    keys = {k for k, _ in NODE_LABELS}
    # Studio's _STEP_TO_NODE writes pass1..pass4 + persona, magnitude, sot, done.
    assert {"pass1", "pass2", "pass3", "pass4",
            "persona", "magnitude", "sot", "done"} <= keys
    assert len(NODE_LABELS) == 9


def test_status_strip_construct_and_set() -> None:
    """Construct a StatusStrip and flip a node — proves the live API works."""
    from enhancer.ui.components.status_strip import StatusStrip

    strip = StatusStrip()
    # set() on a known key shouldn't raise; on an unknown key it's a no-op.
    strip.set("pass1", "running")
    strip.set("pass1", "done")
    strip.set("does-not-exist", "running")  # silent ignore
    strip.reset()


# ─── diff_view ───────────────────────────────────────────────────────────


def test_diff_view_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.components.diff_view")
    assert callable(getattr(mod, "render_diff", None))


def test_diff_view_renders_with_content() -> None:
    """Both texts present — render produces a real difflib HTML table."""
    from enhancer.ui.components.diff_view import render_diff

    # Should not raise; content is dropped into the auto-slot.
    render_diff("hello world", "hello there")


def test_diff_view_renders_empty_inputs() -> None:
    """Empty inputs hit the early-return ``No content to diff.`` path."""
    from enhancer.ui.components.diff_view import render_diff

    render_diff("", "")


# ─── branch_tree ─────────────────────────────────────────────────────────


def test_branch_tree_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.components.branch_tree")
    assert callable(getattr(mod, "render_branch_tree", None))


def test_branch_tree_label_for_helper() -> None:
    """``_label_for`` formats the visible tree label per the schema."""
    from enhancer.ui.components.branch_tree import _label_for

    label = _label_for(
        {"id": "abc12345", "task_type": "coding", "parent_pass": 2,
         "improvement": 50},
        current=True,
    )
    assert "abc12345" in label
    assert "coding" in label
    assert "P2" in label  # forked@P2 marker
    assert "50" in label

    blank = _label_for(
        {"id": "x", "task_type": None, "parent_pass": None, "improvement": None},
        current=False,
    )
    assert "x" in blank


def test_branch_tree_load_lineage_empty(ui_tmp_db) -> None:
    """``_load_lineage`` returns ``[]`` when the run id isn't in the DB."""
    from enhancer.ui.components.branch_tree import _load_lineage

    assert _load_lineage(ui_tmp_db, "nonexistent-run-id") == []


def test_branch_tree_render_handles_missing_run(ui_tmp_db) -> None:
    """``render_branch_tree`` should render the no-lineage fallback label."""
    from enhancer.ui.components.branch_tree import render_branch_tree

    render_branch_tree(ui_tmp_db, "missing-id")  # must not raise


# ─── pass_card ───────────────────────────────────────────────────────────


def test_pass_card_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.components.pass_card")
    assert callable(getattr(mod, "render_pass_card", None))


def test_pass_card_helpers() -> None:
    """``_fmt_duration`` and ``_truncate_model`` are pure utility helpers."""
    from enhancer.ui.components.pass_card import _fmt_duration, _truncate_model

    assert _fmt_duration(450) == "450 ms"
    assert _fmt_duration(1500).endswith("s")
    assert "ms" not in _fmt_duration(2500)

    assert _truncate_model("short") == "short"
    long = "x" * 50
    truncated = _truncate_model(long, n=30)
    assert len(truncated) == 30
    assert truncated.endswith("…")  # ellipsis


def test_pass_card_render_returns_element() -> None:
    """``render_pass_card`` must return the outer ``ui.element``."""
    from enhancer.ui.components.pass_card import render_pass_card

    card = render_pass_card(
        pass_number=1, pass_name="Reframe", content="hello",
        model="fake-7b", duration_ms=500, task_type="coding",
        technique="step-by-step",
        scores={"specificity": 7, "constraints": 5,
                "actionability": 8, "improvement": 60},
    )
    assert card is not None


def test_pass_card_render_error_branch() -> None:
    """The error path renders a ``⚠`` label instead of content markdown."""
    from enhancer.ui.components.pass_card import render_pass_card

    card = render_pass_card(
        pass_number=2, pass_name="Refine", content="",
        error="provider timeout",
    )
    assert card is not None


# ─── score_chips ─────────────────────────────────────────────────────────


def test_score_chips_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.components.score_chips")
    assert callable(getattr(mod, "render_score_chips", None))


@pytest.mark.parametrize("value, scale10, expected", [
    (None, True, "grey"),
    (2,    True, "red"),
    (5,    True, "amber"),
    (8,    True, "green"),
    (10,   False, "red"),    # improvement <30
    (45,   False, "amber"),
    (80,   False, "green"),
])
def test_score_chips_band_for(value, scale10, expected) -> None:
    """Band thresholds are part of the user-visible API; lock them down."""
    from enhancer.ui.components.score_chips import _band_for

    assert _band_for(value, scale10=scale10) == expected


def test_score_chips_render_with_full_set() -> None:
    """All four chips populated — render must not raise."""
    from enhancer.ui.components.score_chips import render_score_chips

    render_score_chips({
        "specificity": 8, "constraints": 6,
        "actionability": 7, "improvement": 55,
    })


def test_score_chips_render_with_missing_keys() -> None:
    """Missing keys render as ``— `` chips, no exception."""
    from enhancer.ui.components.score_chips import render_score_chips

    render_score_chips({})  # empty dict is valid input per docstring


# ─── session_drawer ──────────────────────────────────────────────────────


def test_session_drawer_module_imports() -> None:
    mod = importlib.import_module("enhancer.ui.components.session_drawer")
    assert hasattr(mod, "SessionDrawer")
    assert callable(getattr(mod, "session_context_for", None))


def test_session_drawer_construct(ui_tmp_db) -> None:
    """``SessionDrawer(db_path)`` must initialize without touching the UI tree."""
    from enhancer.ui.components.session_drawer import SessionDrawer

    drawer = SessionDrawer(ui_tmp_db)
    assert drawer.active_id is None
    assert drawer._list_container is None  # render() not called yet
    drawer.on_change(lambda _sid: None)


def test_session_context_for_empty(ui_tmp_db) -> None:
    """No session id → empty string (skips the DB read entirely)."""
    from enhancer.ui.components.session_drawer import session_context_for

    assert session_context_for(ui_tmp_db, None) == ""
    # Also: bogus session id returns "" (falls through ``if not session``).
    assert session_context_for(ui_tmp_db, "no-such-session") == ""


def test_session_drawer_render_smoke(ui_tmp_db) -> None:
    """Full ``render()`` against an empty DB exercises the right_drawer path."""
    from enhancer.ui.components.session_drawer import SessionDrawer

    drawer = SessionDrawer(ui_tmp_db)
    drawer.render()
    # After render, the list_container is created and refresh ran once.
    assert drawer._list_container is not None
