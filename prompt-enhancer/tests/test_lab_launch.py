"""Unit tests for ``lab/launch.py`` helpers.

Subprocess plumbing isn't exercised here (that's the orchestrator's
job). What's worth testing:

* ``_resolve_url`` falls back to ``DEFAULT_URLS`` when no
  ``services.toml`` is present.
* ``_resolve_url`` honors a TOML override when one exists.
* ``_check_config`` reports an unbootable component without spawning.

The launch module isn't an installed package — it sits at
``<APL_ROOT>/lab/launch.py``. We load it via ``importlib`` rather than
poke ``sys.path`` permanently.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


APL_ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCH_PY = APL_ROOT / "lab" / "launch.py"


def _load_launch_module():
    if not LAUNCH_PY.exists():
        pytest.skip("lab/launch.py not present (umbrella checkout only)")
    spec = importlib.util.spec_from_file_location("apl_lab_launch", LAUNCH_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_url_uses_defaults_when_toml_missing(monkeypatch):
    launch = _load_launch_module()

    monkeypatch.setattr(launch, "_read_services_toml", lambda: {})
    assert launch._resolve_url("prompt_enhancer") == "http://127.0.0.1:8765"
    assert launch._resolve_url("round_robin") == "http://127.0.0.1:8766"
    assert launch._resolve_url("development") == "http://127.0.0.1:8767"


def test_resolve_url_honors_toml_override(monkeypatch):
    launch = _load_launch_module()

    monkeypatch.setattr(
        launch,
        "_read_services_toml",
        lambda: {"round_robin": "http://10.0.0.5:9000"},
    )
    assert launch._resolve_url("round_robin") == "http://10.0.0.5:9000"
    # Unmentioned components still fall back to defaults.
    assert launch._resolve_url("prompt_enhancer") == "http://127.0.0.1:8765"


def test_resolve_url_unknown_component_returns_empty(monkeypatch):
    launch = _load_launch_module()

    monkeypatch.setattr(launch, "_read_services_toml", lambda: {})
    assert launch._resolve_url("nonexistent_sibling") == ""


def test_check_config_returns_zero_when_all_present(capsys):
    """`--check` returns 0 if every component's cwd + venv python exist on
    disk. On the dev machine this is the green path."""
    launch = _load_launch_module()

    rc = launch._check_config(list(launch.COMPONENTS.keys()))
    out = capsys.readouterr().out

    # All components on this dev machine are bootable.
    assert rc == 0
    assert "all components ready to boot" in out


def test_check_config_reports_missing_venv(monkeypatch, tmp_path, capsys):
    """When a component's venv is absent, `_check_config` returns non-zero
    and the report names the missing python executable."""
    launch = _load_launch_module()

    fake_components = {
        "fake_component": {
            "cwd": tmp_path,  # exists, but no .venv inside it
            "command": [".venv/Scripts/python.exe", "-m", "fake"],
            "health_path": "/api/health",
        }
    }
    monkeypatch.setattr(launch, "COMPONENTS", fake_components)
    monkeypatch.setattr(
        launch, "DEFAULT_URLS", {"fake_component": "http://127.0.0.1:9999"}
    )

    rc = launch._check_config(["fake_component"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "MISSING" in out
    assert "fake_component" in out
