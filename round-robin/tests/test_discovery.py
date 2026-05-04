"""Tests for ``round_robin.discovery`` — services.toml lookup.

Mirror of ``prompt-enhancer/tests/test_discovery.py``. The two
products share a config file format so their discovery modules
must behave identically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from round_robin import discovery


@pytest.fixture(autouse=True)
def _isolate_services_path(tmp_path, monkeypatch):
    """Redirect ``services_path()`` to a fresh tmp file per test."""
    fake = tmp_path / "services.toml"
    monkeypatch.setattr(discovery, "services_path", lambda: fake)
    return fake


def test_defaults_when_no_file(_isolate_services_path):
    assert discovery.get_peer_url("prompt_enhancer") == "http://127.0.0.1:8765"
    assert discovery.get_peer_url("round_robin") == "http://127.0.0.1:8766"
    assert discovery.get_peer_url("interpreter") == "http://127.0.0.1:8767"


def test_unknown_peer_with_default(_isolate_services_path):
    assert (
        discovery.get_peer_url("nonexistent", default="http://example.com:9999")
        == "http://example.com:9999"
    )


def test_unknown_peer_no_default_returns_empty(_isolate_services_path):
    assert discovery.get_peer_url("nonexistent") == ""


def test_overrides_from_toml(_isolate_services_path: Path):
    _isolate_services_path.write_text(
        "[services]\n"
        'prompt_enhancer = "http://192.168.1.50:8765"\n'
        'round_robin     = "http://192.168.1.51:8766/"\n',  # trailing slash stripped
        encoding="utf-8",
    )
    assert (
        discovery.get_peer_url("prompt_enhancer")
        == "http://192.168.1.50:8765"
    )
    assert (
        discovery.get_peer_url("round_robin")
        == "http://192.168.1.51:8766"
    )
    # Unspecified peers fall back to defaults
    assert discovery.get_peer_url("interpreter") == "http://127.0.0.1:8767"


def test_get_all_peers_merges_overrides(_isolate_services_path: Path):
    _isolate_services_path.write_text(
        "[services]\nprompt_enhancer = \"http://override:1\"\n",
        encoding="utf-8",
    )
    peers = discovery.get_all_peers()
    assert peers["prompt_enhancer"] == "http://override:1"
    assert peers["round_robin"] == "http://127.0.0.1:8766"
    assert peers["interpreter"] == "http://127.0.0.1:8767"


def test_get_all_peers_includes_all_defaults_when_no_file(_isolate_services_path):
    peers = discovery.get_all_peers()
    assert peers == {
        "prompt_enhancer": "http://127.0.0.1:8765",
        "round_robin": "http://127.0.0.1:8766",
        "interpreter": "http://127.0.0.1:8767",
    }


def test_malformed_toml_falls_back_to_defaults(_isolate_services_path: Path):
    _isolate_services_path.write_text("this is not = valid !! toml ::", encoding="utf-8")
    # Bad config must NOT crash startup.
    assert discovery.get_peer_url("prompt_enhancer") == "http://127.0.0.1:8765"
    # get_all_peers must also recover.
    peers = discovery.get_all_peers()
    assert peers["round_robin"] == "http://127.0.0.1:8766"


def test_defaults_match_prompt_enhancer():
    """The two products MUST agree on each other's locations.

    If this test fails, prompt-enhancer's discovery.DEFAULTS has drifted
    from round-robin's — the cross-product loop will misroute requests.
    Update both halves together.
    """
    assert discovery.DEFAULTS == {
        "prompt_enhancer": "http://127.0.0.1:8765",
        "round_robin": "http://127.0.0.1:8766",
        "interpreter": "http://127.0.0.1:8767",
    }
