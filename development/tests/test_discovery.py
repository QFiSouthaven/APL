"""Tests for ``development.discovery`` — services.toml lookup.

Mirror of prompt-enhancer's test_discovery.py: same scenarios, same
defaults table, against the local discovery module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from development import discovery


@pytest.fixture(autouse=True)
def _isolate_services_path(tmp_path, monkeypatch):
    """Redirect ``services_path()`` to a fresh tmp file per test."""
    fake = tmp_path / "services.toml"
    monkeypatch.setattr(discovery, "services_path", lambda: fake)
    return fake


def test_defaults_when_no_file(_isolate_services_path):
    assert discovery.get_peer_url("prompt_enhancer") == "http://127.0.0.1:8765"
    assert discovery.get_peer_url("round_robin") == "http://127.0.0.1:8766"
    assert discovery.get_peer_url("development") == "http://127.0.0.1:8767"


def test_unknown_peer_with_default(_isolate_services_path):
    assert (
        discovery.get_peer_url("nonexistent", default="http://example.com:9999")
        == "http://example.com:9999"
    )


def test_unknown_peer_with_no_default_returns_empty(_isolate_services_path):
    assert discovery.get_peer_url("nonexistent") == ""


def test_overrides_from_toml(_isolate_services_path: Path):
    _isolate_services_path.write_text(
        "[services]\n"
        'prompt_enhancer = "http://192.168.1.50:8765"\n'
        'round_robin     = "http://192.168.1.51:8766/"\n',  # trailing slash stripped
        encoding="utf-8",
    )
    assert discovery.get_peer_url("prompt_enhancer") == "http://192.168.1.50:8765"
    assert discovery.get_peer_url("round_robin") == "http://192.168.1.51:8766"
    # Unspecified peers fall back to defaults
    assert discovery.get_peer_url("development") == "http://127.0.0.1:8767"


def test_get_all_peers_merges_overrides(_isolate_services_path: Path):
    _isolate_services_path.write_text(
        '[services]\nprompt_enhancer = "http://override:1"\n',
        encoding="utf-8",
    )
    peers = discovery.get_all_peers()
    assert peers["prompt_enhancer"] == "http://override:1"
    assert peers["round_robin"] == "http://127.0.0.1:8766"
    assert peers["development"] == "http://127.0.0.1:8767"


def test_malformed_toml_falls_back_to_defaults(_isolate_services_path: Path):
    _isolate_services_path.write_text(
        "this is not = valid !! toml ::", encoding="utf-8"
    )
    # Bad config must NOT crash startup.
    assert discovery.get_peer_url("prompt_enhancer") == "http://127.0.0.1:8765"


def test_defaults_dict_is_byte_for_byte_with_peers():
    """The DEFAULTS dict must match the other two products exactly."""
    assert discovery.DEFAULTS == {
        "prompt_enhancer": "http://127.0.0.1:8765",
        "round_robin": "http://127.0.0.1:8766",
        "development": "http://127.0.0.1:8767",
    }
