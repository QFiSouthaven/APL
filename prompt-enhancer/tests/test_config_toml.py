"""TOML settings persistence — precedence, round-trip, and corruption fallback.

These tests cover the three guarantees of ``enhancer.config``:

1. ``load()`` layers defaults < TOML file < env vars.
2. ``save_settings()`` then ``load()`` round-trips byte-for-byte.
3. A malformed TOML file does not raise — ``load()`` returns defaults.

We override ``config.settings_path`` via ``monkeypatch`` so the real user
config dir is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from enhancer import config


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path) -> Path:
    """Redirect ``settings_path()`` to a tmp file and clear any env overrides.

    Returns the tmp path so tests can write/read it directly.
    """
    target = tmp_path / "settings.toml"
    monkeypatch.setattr(config, "settings_path", lambda: target)

    # Strip every ENHANCER_* env var so the OS environment doesn't leak in.
    for suffix in config._ENV_SUFFIX.values():
        monkeypatch.delenv(f"ENHANCER_{suffix}", raising=False)

    return target


def test_load_precedence(isolated_settings: Path, monkeypatch) -> None:
    """Defaults < TOML < env, verified at three layered checkpoints."""
    # Layer 0: pure defaults (no TOML, no env).
    s = config.load()
    defaults = config.Settings()
    assert s.temperature == defaults.temperature
    assert s.default_model == defaults.default_model
    assert s.provider == defaults.provider

    # Layer 1: TOML overrides defaults.
    isolated_settings.write_text(
        'temperature = 0.42\n'
        'default_model = "from-toml"\n'
        'provider = "ollama"\n',
        encoding="utf-8",
    )
    s = config.load()
    assert s.temperature == 0.42
    assert s.default_model == "from-toml"
    assert s.provider == "ollama"

    # Layer 2: env overrides TOML (and defaults).
    monkeypatch.setenv("ENHANCER_TEMPERATURE", "1.25")
    monkeypatch.setenv("ENHANCER_DEFAULT_MODEL", "from-env")
    s = config.load()
    assert s.temperature == 1.25
    assert s.default_model == "from-env"
    # Provider not overridden by env → still TOML value.
    assert s.provider == "ollama"


def test_save_then_load_round_trips(isolated_settings: Path) -> None:
    """``save_settings`` → ``load`` returns an identical Settings object."""
    original = config.Settings(
        provider="anthropic",
        lms_base_url="http://localhost:9999/v1",
        lms_management_url="http://localhost:9999",
        default_model="claude-test",
        scorer_model="claude-scorer",
        request_timeout=300.0,
        idle_timeout=90.0,
        temperature=0.33,
        max_tokens_scale=1.75,
        disambiguate_threshold=5,
        ui_port=9876,
        ui_host="0.0.0.0",
        methodology_agent_enabled=False,
    )

    written = config.save_settings(original)
    assert written == isolated_settings.resolve()
    assert isolated_settings.exists()

    reloaded = config.load()
    assert reloaded == original


def test_malformed_toml_falls_back_to_defaults(isolated_settings: Path) -> None:
    """Garbage in the TOML file → ``load()`` returns defaults, no exception."""
    isolated_settings.write_text(
        "this is not [valid toml = \x00\x01 ::: \n[[[",
        encoding="utf-8",
    )

    # Must not raise.
    s = config.load()
    assert s == config.Settings()
