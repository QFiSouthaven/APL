"""Unit tests for the health.py model→host mapping."""
from round_robin.health import _build_remote_model_map, _summarize_model


def test_build_remote_model_map_empty():
    assert _build_remote_model_map(None) == {}
    assert _build_remote_model_map({"remote_devices": []}) == {}


def test_build_remote_model_map_one_remote():
    link = {
        "remote_devices": [
            {"name": "m5", "loaded_models": ["gpt-oss-120b-abliterated-i1", "llama-3"]},
        ]
    }
    assert _build_remote_model_map(link) == {
        "gpt-oss-120b-abliterated-i1": "m5",
        "llama-3": "m5",
    }


def test_build_remote_model_map_multiple_remotes():
    link = {
        "remote_devices": [
            {"name": "m5", "loaded_models": ["model-a"]},
            {"name": "laptop", "loaded_models": ["model-b", "model-c"]},
        ]
    }
    out = _build_remote_model_map(link)
    assert out["model-a"] == "m5"
    assert out["model-b"] == "laptop"
    assert out["model-c"] == "laptop"


def test_summarize_model_remote_match():
    """Model loaded on m5 → tagged as remote."""
    m = {"id": "gpt-oss-120b-abliterated-i1"}
    out = _summarize_model(m, local_device="DESKTOP-NDMJ1VD",
                          remote_model_map={"gpt-oss-120b-abliterated-i1": "m5"})
    assert out["is_local"] is False
    assert out["device"] == "m5"


def test_summarize_model_local_when_not_in_remote_map():
    m = {"id": "gemma-local"}
    out = _summarize_model(m, local_device="DESKTOP", remote_model_map={"other": "m5"})
    assert out["is_local"] is True
    assert out["device"] == "DESKTOP"


def test_summarize_model_no_link_data_falls_back_to_local():
    m = {"id": "anything"}
    out = _summarize_model(m, local_device=None, remote_model_map={})
    assert out["is_local"] is True


def test_summarize_model_inline_device_tag_respected():
    """If the model entry itself carries a device tag matching local, treat as local."""
    m = {"id": "x", "device": "DESKTOP"}
    out = _summarize_model(m, local_device="DESKTOP", remote_model_map={})
    assert out["is_local"] is True
    assert out["device"] == "DESKTOP"


def test_summarize_model_remote_takes_precedence_over_inline_tag():
    """If lms link status says it's remote, that wins over any stale inline tag."""
    m = {"id": "x", "device": "DESKTOP"}     # bogus self-tag
    out = _summarize_model(m, local_device="DESKTOP",
                          remote_model_map={"x": "m5"})
    assert out["is_local"] is False
    assert out["device"] == "m5"
