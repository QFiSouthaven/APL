from pathlib import Path

import pytest

from round_robin.charlie.workspace import CharlieWorkspace, SandboxError


def make_ws(tmp_path: Path) -> CharlieWorkspace:
    return CharlieWorkspace(base_dir=tmp_path)


def test_write_creates_file(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    p = ws.write("hello.py", "print('hi')")
    assert p.read_text() == "print('hi')"
    assert "hello.py" in [c["name"] for c in ws.tree()["children"]]


def test_traversal_rejected(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with pytest.raises(SandboxError):
        ws.write("../escape.py", "boom")


def test_absolute_path_rejected(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with pytest.raises(SandboxError):
        ws.write("/etc/passwd", "x")


def test_drive_letter_rejected(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with pytest.raises(SandboxError):
        ws.write("C:/Windows/System32/drivers/etc/hosts", "x")


def test_hidden_path_rejected(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with pytest.raises(SandboxError):
        ws.write(".env", "secret=1")


def test_size_cap(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    big = "a" * (3 * 1024 * 1024)  # 3 MB > 2 MB cap
    with pytest.raises(SandboxError):
        ws.write("big.txt", big)


def test_delete_only_owned_files(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.write("mine.txt", "Charlie wrote this")
    foreign = ws.root / "foreign.txt"
    foreign.write_text("not Charlie's")
    assert ws.delete("mine.txt") is True
    with pytest.raises(SandboxError):
        ws.delete("foreign.txt")


def test_read_returns_text(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.write("src/main.py", "print(1)")
    data = ws.read("src/main.py")
    assert data["content"] == "print(1)"
    assert data["binary"] is False


def test_read_binary_extension(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    p = ws.root / "x.png"
    p.write_bytes(b"\x89PNG\r\n")
    data = ws.read("x.png")
    assert data["binary"] is True


def test_clear(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.write("a.txt", "1")
    ws.write("b/c.txt", "2")
    ws.clear()
    assert ws.tree()["children"] == []
