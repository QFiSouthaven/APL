"""Smoke tests for the static SPA shell at ``static/index.html``."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

INDEX = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "development"
    / "static"
    / "index.html"
)


def test_index_html_exists_and_not_empty():
    assert INDEX.exists(), f"static UI file missing: {INDEX}"
    text = INDEX.read_text(encoding="utf-8")
    assert len(text) > 1000, "static index.html looks like a placeholder"


class _Collector(HTMLParser):
    """Collect tag names and their id attributes for shape assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: set[str] = set()
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)

    def error(self, message):  # type: ignore[override]
        # html.parser is forgiving but exposes errors via this hook on
        # older Pythons; record rather than raise so we keep parsing.
        self.errors.append(message)


def _parse() -> _Collector:
    p = _Collector()
    p.feed(INDEX.read_text(encoding="utf-8"))
    return p


def test_required_ui_elements_present():
    """The HTML must contain the form elements the integration depends on."""
    p = _parse()
    # Top-level structure
    assert "form" in p.tags
    assert "textarea" in p.tags
    assert "button" in p.tags
    assert "script" in p.tags
    assert "style" in p.tags

    required_ids = {
        "goal",
        "stack-hint",
        "target-lang",
        "build-form",
        "submit-btn",
        "event-log",
        "result-panel",
        "version-badge",
        "peers-strip",
    }
    missing = required_ids - p.ids
    assert not missing, f"missing required ids in index.html: {sorted(missing)}"


def test_html_references_apl_endpoints():
    """The page must wire to the four APL endpoints we expose."""
    text = INDEX.read_text(encoding="utf-8")
    for endpoint in ("/api/build", "/api/events", "/api/health", "/api/peers"):
        assert endpoint in text, f"index.html does not reference {endpoint}"


def test_html_uses_eventsource_for_live_updates():
    """Verify the SSE wiring is in place (rather than e.g. polling /api/runs)."""
    text = INDEX.read_text(encoding="utf-8")
    assert re.search(r"\bnew\s+EventSource\b", text), \
        "expected EventSource() usage for SSE wiring"
