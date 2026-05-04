from pathlib import Path

import pytest

from round_robin import user_config
from round_robin import config as rr_config


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE so each test gets a clean slate."""
    f = tmp_path / "config.json"
    monkeypatch.setattr(rr_config, "CONFIG_FILE", f)
    monkeypatch.setattr(user_config, "CONFIG_FILE", f)
    yield


def test_load_returns_defaults_when_missing():
    cfg = user_config.load()
    assert cfg["intel_collab_directive"] is True
    assert cfg["intel_anti_rambling"] is True
    assert cfg["intel_anti_yes_man"] is True
    assert cfg["intel_agreement_threshold"] == 2
    assert cfg["loop_limit"] == 3
    assert cfg["theme"] == ""


def test_save_then_load_round_trips():
    user_config.save({"theme": "Plan a trip", "intel_anti_rambling": False, "loop_limit": 12})
    cfg = user_config.load()
    assert cfg["theme"] == "Plan a trip"
    assert cfg["intel_anti_rambling"] is False
    assert cfg["loop_limit"] == 12
    # Untouched defaults are preserved
    assert cfg["intel_anti_yes_man"] is True


def test_partial_patch_preserves_other_keys():
    user_config.save({"theme": "First"})
    user_config.save({"loop_limit": 7})       # different key, partial update
    cfg = user_config.load()
    assert cfg["theme"] == "First"
    assert cfg["loop_limit"] == 7


def test_unknown_keys_are_dropped():
    user_config.save({"theme": "ok", "evil_key": "should not stick"})
    cfg = user_config.load()
    assert cfg["theme"] == "ok"
    assert "evil_key" not in cfg


def test_save_rejects_non_dict():
    with pytest.raises(ValueError):
        user_config.save("not a dict")  # type: ignore[arg-type]


def test_reset_returns_defaults_and_persists(tmp_path):
    user_config.save({"theme": "scribble"})
    out = user_config.reset()
    assert out["theme"] == ""
    assert user_config.load()["theme"] == ""


def test_load_recovers_from_corrupt_file():
    user_config.save({"theme": "good"})    # creates .json + .bak baseline
    user_config.save({"theme": "newer"})   # rotates good -> .bak
    rr_config.CONFIG_FILE.write_text("{not json", encoding="utf-8")
    cfg = user_config.load()
    # SafeStorage falls back to .bak which holds the FIRST save
    assert cfg["theme"] == "good"
