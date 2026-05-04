from pathlib import Path

from round_robin.storage import SafeStorage


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "out.json"
    SafeStorage.save_json(f, {"a": 1, "b": [1, 2, 3]})
    assert SafeStorage.load_json(f, None) == {"a": 1, "b": [1, 2, 3]}


def test_load_missing_returns_default(tmp_path: Path) -> None:
    assert SafeStorage.load_json(tmp_path / "missing.json", {"x": 9}) == {"x": 9}


def test_corrupt_falls_back_to_bak(tmp_path: Path) -> None:
    f = tmp_path / "out.json"
    SafeStorage.save_json(f, {"v": 1})            # writes .json
    SafeStorage.save_json(f, {"v": 2})            # writes .json, copies prior to .bak
    f.write_text("{not json", encoding="utf-8")   # corrupt the primary
    loaded = SafeStorage.load_json(f, {"fallback": True})
    assert loaded == {"v": 1}


def test_corrupt_no_bak_returns_default(tmp_path: Path) -> None:
    f = tmp_path / "out.json"
    f.write_text("garbage", encoding="utf-8")
    assert SafeStorage.load_json(f, {"d": 1}) == {"d": 1}
