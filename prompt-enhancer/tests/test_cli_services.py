"""Tests for the ``enhancer services`` subcommands.

The discovery layer is unchanged — we only verify the CLI surface that
shows / writes / locates ``services.toml``. Each test isolates the path
to ``tmp_path`` via monkeypatch on :func:`enhancer.api.discovery.services_path`
so we never touch the user's real ``%APPDATA%\\swarm`` directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enhancer.api import discovery
from enhancer.cli import _services as svc_module
from enhancer.cli.main import app

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


@pytest.fixture
def isolated_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``services_path()`` to a fresh tmp file for the test."""
    fake = tmp_path / "swarm" / "services.toml"
    # Patch in BOTH the source module and the CLI module's import
    # binding — discovery.services_path is rebound inside _services.py
    # via ``from ..api import discovery`` so the attribute lookup goes
    # through the discovery module each call. One patch suffices.
    monkeypatch.setattr(discovery, "services_path", lambda: fake)
    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── show ────────────────────────────────────────────────────────────

def test_services_show_prints_defaults_when_no_file(
    isolated_path: Path, runner: CliRunner,
) -> None:
    assert not isolated_path.exists()
    result = runner.invoke(app, ["services", "show"])
    assert result.exit_code == 0, result.output
    # All three default peers appear with their default URLs.
    assert "prompt_enhancer" in result.output
    assert "http://127.0.0.1:8765" in result.output
    assert "round_robin" in result.output
    assert "http://127.0.0.1:8766" in result.output
    assert "development" in result.output
    assert "http://127.0.0.1:8767" in result.output
    # File-status line is informative.
    assert "DEFAULTS only" in result.output or "no" in result.output.lower()


def test_services_show_merges_file(
    isolated_path: Path, runner: CliRunner,
) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text(
        '[services]\nround_robin = "http://10.0.0.99:8766"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["services", "show"])
    assert result.exit_code == 0, result.output
    # Override is reflected.
    assert "http://10.0.0.99:8766" in result.output
    # Non-overridden peers still come from DEFAULTS.
    assert "http://127.0.0.1:8765" in result.output


# ── init ────────────────────────────────────────────────────────────

def test_services_init_creates_file_when_absent(
    isolated_path: Path, runner: CliRunner,
) -> None:
    assert not isolated_path.exists()
    assert not isolated_path.parent.exists()  # parent dir auto-created

    result = runner.invoke(app, ["services", "init"])
    assert result.exit_code == 0, result.output
    assert isolated_path.exists()

    # File parses as valid TOML.
    with isolated_path.open("rb") as f:
        data = tomllib.load(f)

    # [services] table present with all three defaults.
    assert "services" in data
    services = data["services"]
    for key, val in discovery.DEFAULTS.items():
        assert services.get(key) == val, (
            f"expected {key}={val!r} in starter file, got {services.get(key)!r}"
        )

    # Header comment is present (sanity check on the literal text).
    text = isolated_path.read_text(encoding="utf-8")
    assert "services.toml" in text
    assert "Precedence" in text
    assert "docs/SERVICES.md" in text


def test_services_init_refuses_to_overwrite_without_force(
    isolated_path: Path, runner: CliRunner,
) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text("# pre-existing\n", encoding="utf-8")

    result = runner.invoke(app, ["services", "init"])
    assert result.exit_code != 0, result.output
    assert "already exists" in result.output
    # File untouched.
    assert isolated_path.read_text(encoding="utf-8") == "# pre-existing\n"


def test_services_init_force_overwrites(
    isolated_path: Path, runner: CliRunner,
) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text("# pre-existing\n", encoding="utf-8")

    result = runner.invoke(app, ["services", "init", "--force"])
    assert result.exit_code == 0, result.output

    # File replaced with starter content (parses, [services] populated).
    with isolated_path.open("rb") as f:
        data = tomllib.load(f)
    assert "services" in data
    assert data["services"]["prompt_enhancer"] == discovery.DEFAULTS["prompt_enhancer"]


# ── path ────────────────────────────────────────────────────────────

def test_services_path_prints_absolute_path(
    isolated_path: Path, runner: CliRunner,
) -> None:
    result = runner.invoke(app, ["services", "path"])
    assert result.exit_code == 0, result.output
    out = result.output.strip()
    # Path matches what services_path() returns, and is absolute.
    assert out == str(isolated_path)
    assert Path(out).is_absolute()


# ── helper rendering ────────────────────────────────────────────────

def test_render_starter_toml_yields_parseable_block_with_all_defaults() -> None:
    """The starter body must parse to a [services] table with every default."""
    body = svc_module._render_starter_toml()
    data = tomllib.loads(body)
    assert "services" in data
    for key, val in discovery.DEFAULTS.items():
        assert data["services"][key] == val
