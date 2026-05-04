"""Tests for ``development.templates`` — stack-template plugin discovery
and the Architect's stack-template fast-path.

The third-party path is exercised by monkeypatching
``_iter_entry_points`` on the ``development.templates`` package — we
don't pip-install fake plugins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from development import templates as templates_pkg
from development.stages.architect import ArchitectStage
from development.templates import StackTemplate, discover_templates
from development.templates._builtin_fastapi_sqlite import FastApiSqliteTemplate
from development.types import STAGE_PROGRESS, BuildRequest

from tests.conftest import FakeLMClient


# ── helpers ─────────────────────────────────────────────────────────


@dataclass
class _FakeEntryPoint:
    name: str
    target: object  # what ``.load()`` returns

    def load(self):
        return self.target


def _patch_entry_points(monkeypatch, eps):
    """Replace ``_iter_entry_points`` so discovery sees ``eps``."""
    monkeypatch.setattr(
        templates_pkg, "_iter_entry_points",
        lambda group: list(eps) if group == "development.stack_templates" else [],
    )


class _SimpleTemplate(StackTemplate):
    """Minimal third-party-style template used in monkeypatched tests."""

    name = "simple"

    def matches(self, stack_hint: str) -> bool:
        return "simple" in stack_hint

    def build_plan(self, request):
        return {
            "stack": {"backend": "simple"},
            "layers": [],
            "dependencies": [],
            "constraints_satisfied": {},
        }


class _NotATemplate:
    """Deliberately not a StackTemplate subclass."""

    pass


# ── discovery ───────────────────────────────────────────────────────


def test_discover_templates_returns_builtin_fastapi_sqlite():
    """Smoke: with no monkeypatching, the built-in entry-point should
    be visible (it's registered in this package's own pyproject.toml,
    so an editable install makes it discoverable)."""
    found = discover_templates()
    # Only assert if the package is installed; otherwise skip cleanly.
    if "fastapi-sqlite" not in found:
        pytest.skip(
            "development package not editable-installed; entry-point "
            "metadata not present. Run `pip install -e .` first."
        )
    assert found["fastapi-sqlite"] is FastApiSqliteTemplate


def test_discover_templates_skips_non_subclass(monkeypatch, caplog):
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="ok", target=_SimpleTemplate),
        _FakeEntryPoint(name="bogus", target=_NotATemplate),
    ])
    with caplog.at_level("WARNING", logger="development.templates"):
        found = discover_templates()
    assert "ok" in found
    assert "bogus" not in found
    assert any(
        "not a StackTemplate subclass" in rec.getMessage()
        for rec in caplog.records
    )


def test_discover_templates_skips_load_failure(monkeypatch, caplog):
    """A template whose ``.load()`` raises is logged + skipped, not propagated."""

    class _ExplodingEP:
        name = "kaboom"

        def load(self):
            raise RuntimeError("plugin import broken")

    _patch_entry_points(
        monkeypatch,
        [_ExplodingEP(), _FakeEntryPoint(name="ok", target=_SimpleTemplate)],
    )
    with caplog.at_level("WARNING", logger="development.templates"):
        found = discover_templates()
    assert "ok" in found
    assert "kaboom" not in found
    assert any(
        "Failed to load entry-point" in rec.getMessage()
        for rec in caplog.records
    )


def test_discover_templates_third_party_registration(monkeypatch):
    """Custom third-party template registration via monkeypatched entry-points."""
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="custom", target=_SimpleTemplate),
    ])
    found = discover_templates()
    assert list(found.keys()) == ["custom"]
    assert found["custom"] is _SimpleTemplate


# ── FastApiSqliteTemplate.matches ───────────────────────────────────


@pytest.mark.parametrize("hint", [
    "fastapi+sqlite",
    "fastapi-sqlite",
    "fastapi with sqlite",
    "FastAPI with SQLite",
    "use fastapi and sqlite3 please",
])
def test_fastapi_sqlite_matches_true(hint):
    tpl = FastApiSqliteTemplate()
    # Architect lowercases before passing in.
    assert tpl.matches(hint.lower()) is True


@pytest.mark.parametrize("hint", [
    "express+postgres",
    "django+mysql",
    "fastapi only",        # no sqlite term
    "sqlite only",         # no fastapi term
    "",
])
def test_fastapi_sqlite_matches_false(hint):
    tpl = FastApiSqliteTemplate()
    assert tpl.matches(hint.lower()) is False


# ── FastApiSqliteTemplate.build_plan ────────────────────────────────


def test_fastapi_sqlite_build_plan_shape():
    tpl = FastApiSqliteTemplate()
    plan = tpl.build_plan(BuildRequest(goal="todo app"))

    # All canonical keys present.
    for k in ("stack", "layers", "dependencies", "constraints_satisfied"):
        assert k in plan, f"plan missing top-level key: {k}"

    assert plan["stack"]["backend"] == "fastapi"
    assert plan["stack"]["database"] == "sqlite"
    assert plan["stack"]["deployment"] == "docker"

    # Dependencies as documented.
    assert "fastapi" in plan["dependencies"]
    assert "uvicorn" in plan["dependencies"]
    assert "sqlalchemy" in plan["dependencies"]

    # Default port when not constrained.
    assert plan["constraints_satisfied"]["port"] == 8000

    # Layers list contains the expected three.
    layer_names = {layer["name"] for layer in plan["layers"]}
    assert layer_names == {"backend", "database", "deployment"}


def test_fastapi_sqlite_build_plan_honors_port_constraint():
    tpl = FastApiSqliteTemplate()
    plan = tpl.build_plan(BuildRequest(goal="x", constraints={"port": 9999}))
    assert plan["constraints_satisfied"]["port"] == 9999


# ── Architect fast-path ─────────────────────────────────────────────


def _ctx_for(goal: str, **kwargs) -> dict:
    return {
        "build_request": BuildRequest(goal=goal, **kwargs),
        "plan": {},
        "artifacts": {},
        "message_board": None,
    }


@pytest.mark.asyncio
async def test_architect_fast_path_skips_llm_when_template_matches(monkeypatch):
    """When stack_hint matches a template, the LLM must NOT be called."""
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="fastapi-sqlite", target=FastApiSqliteTemplate),
    ])

    fake = FakeLMClient(responses=[])  # would error if popped
    stage = ArchitectStage(fake)

    ctx = _ctx_for("a todo app", stack_hint="fastapi+sqlite")
    out = await stage.run(ctx)

    # LLM never called.
    assert fake.calls == []
    # Plan came from the template, with the canonical normalized shape.
    assert out["plan"]["stack"]["backend"] == "fastapi"
    assert out["plan"]["stack"]["database"] == "sqlite"
    assert out["plan_source"] == "template:fastapi-sqlite"
    # Normalize-on-template ensures canonical keys exist.
    for k in ("stack", "layers", "dependencies", "constraints_satisfied"):
        assert k in out["plan"]


@pytest.mark.asyncio
async def test_architect_falls_through_to_llm_when_no_template_matches(monkeypatch):
    """When stack_hint matches no template, the LLM path runs as before."""
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="fastapi-sqlite", target=FastApiSqliteTemplate),
    ])

    plan = {
        "stack": {"backend": "express"},
        "layers": [],
        "dependencies": [],
        "constraints_satisfied": {},
    }
    fake = FakeLMClient(responses=[json.dumps(plan)])
    stage = ArchitectStage(fake)

    ctx = _ctx_for("a node app", stack_hint="express+postgres")
    out = await stage.run(ctx)

    # LLM was called exactly once (no retry).
    assert len(fake.calls) == 1
    assert out["plan"]["stack"] == {"backend": "express"}
    # No plan_source stamped — that's the LLM-path signature.
    assert "plan_source" not in out


@pytest.mark.asyncio
async def test_architect_fast_path_publishes_stage_progress(monkeypatch):
    """Fast-path must publish a STAGE_PROGRESS event with template metadata."""
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="fastapi-sqlite", target=FastApiSqliteTemplate),
    ])

    published: list[tuple[str, dict]] = []

    class _FakeBoard:
        def publish(self, kind, payload):
            published.append((kind, payload))

    fake = FakeLMClient(responses=[])
    stage = ArchitectStage(fake)

    ctx = _ctx_for("hi", stack_hint="fastapi-sqlite")
    ctx["message_board"] = _FakeBoard()
    await stage.run(ctx)

    assert published, "expected a STAGE_PROGRESS event"
    kind, payload = published[0]
    assert kind == STAGE_PROGRESS
    assert payload["stage"] == "architect"
    assert payload["source"] == "template"
    assert payload["template"] == "fastapi-sqlite"


@pytest.mark.asyncio
async def test_architect_no_hint_skips_template_dispatch(monkeypatch):
    """When stack_hint is None/empty, templates are not consulted at all."""
    called = {"discover": 0}

    def _spy(group):
        called["discover"] += 1
        return []

    monkeypatch.setattr(templates_pkg, "_iter_entry_points", _spy)

    plan = {"stack": {}, "layers": [], "dependencies": [], "constraints_satisfied": {}}
    fake = FakeLMClient(responses=[json.dumps(plan)])
    stage = ArchitectStage(fake)

    await stage.run(_ctx_for("just a goal"))  # no stack_hint

    assert called["discover"] == 0
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_architect_fast_path_with_third_party_template(monkeypatch):
    """A third-party template registered at runtime drives the fast-path."""
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="simple", target=_SimpleTemplate),
    ])

    fake = FakeLMClient(responses=[])
    stage = ArchitectStage(fake)

    out = await stage.run(_ctx_for("x", stack_hint="my simple stack"))
    assert fake.calls == []
    assert out["plan"]["stack"] == {"backend": "simple"}
    assert out["plan_source"] == "template:simple"
