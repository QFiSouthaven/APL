"""Tests for the lms link status parser."""
from round_robin.lms_cli import _parse_link_status


REAL_OUTPUT_ONE_REMOTE = """\
This device: DESKTOP-NDMJ1VD
Status: Online

Found 1 device:

  - m5
    Status: connected
    Identifier: fa59d011d0477706fdd14b238cc5dd44
    Loaded Models Instances:
      - gpt-oss-120b-abliterated-i1
"""

REAL_OUTPUT_TWO_REMOTES = """\
This device: DESKTOP-NDMJ1VD
Status: Online

Found 2 devices:

  - m5
    Status: connected
    Identifier: fa59d011d0477706fdd14b238cc5dd44
    Loaded Models Instances:
      - gpt-oss-120b-abliterated-i1
      - llama-3.1-8b-q4

  - laptop-bravo
    Status: connected
    Identifier: deadbeef0000111122223333
    Loaded Models Instances:
      - mistral-7b-instruct
"""


def test_parse_real_one_remote():
    out = _parse_link_status(REAL_OUTPUT_ONE_REMOTE)
    assert out["enabled"] is True
    assert out["this_device"] == "DESKTOP-NDMJ1VD"
    assert len(out["remote_devices"]) == 1
    dev = out["remote_devices"][0]
    assert dev["name"] == "m5"
    assert dev["identifier"] == "fa59d011d0477706fdd14b238cc5dd44"
    assert dev["status"] == "connected"
    assert dev["loaded_models"] == ["gpt-oss-120b-abliterated-i1"]
    # Backward-compat flat list
    assert "DESKTOP-NDMJ1VD" in out["devices"]
    assert "m5" in out["devices"]


def test_parse_two_remotes_with_multiple_models():
    out = _parse_link_status(REAL_OUTPUT_TWO_REMOTES)
    assert out["enabled"] is True
    assert out["this_device"] == "DESKTOP-NDMJ1VD"
    assert len(out["remote_devices"]) == 2
    names = sorted(d["name"] for d in out["remote_devices"])
    assert names == ["laptop-bravo", "m5"]
    m5 = next(d for d in out["remote_devices"] if d["name"] == "m5")
    assert m5["loaded_models"] == ["gpt-oss-120b-abliterated-i1", "llama-3.1-8b-q4"]
    laptop = next(d for d in out["remote_devices"] if d["name"] == "laptop-bravo")
    assert laptop["loaded_models"] == ["mistral-7b-instruct"]
    assert laptop["identifier"] == "deadbeef0000111122223333"


def test_parse_empty_returns_disabled():
    out = _parse_link_status("")
    assert out == {
        "raw": "",
        "enabled": False,
        "this_device": None,
        "remote_devices": [],
        "devices": [],
    }


def test_parse_explicit_disabled_text():
    out = _parse_link_status("LM Link is disabled")
    assert out["enabled"] is False
    assert out["remote_devices"] == []


def test_parse_no_remotes_yet():
    """LM Link enabled, this device known, no peers paired."""
    out = _parse_link_status("This device: SOLO-PC\nStatus: Online\n")
    assert out["enabled"] is True
    assert out["this_device"] == "SOLO-PC"
    assert out["remote_devices"] == []
    assert out["devices"] == ["SOLO-PC"]


def test_parse_handles_unknown_extra_keys():
    """Extra metadata lines shouldn't break parsing."""
    text = """\
This device: HOST
Status: Online

Found 1 device:

  - peer
    Status: connected
    Identifier: abc123
    Some New Key: some value
    Loaded Models Instances:
      - model-a
"""
    out = _parse_link_status(text)
    assert out["remote_devices"][0]["loaded_models"] == ["model-a"]
