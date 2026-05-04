"""Tests for the v2.0 Coder tool catalog (development.tools)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from development.tools import (
    MAX_TOOL_CALLS_PER_LAYER,
    TOOL_CATALOG,
    TOOL_DISPATCH,
    dispatch_tool_call,
)
from development.tools.exec import _ALLOWED_BINS, sandboxed_exec
from development.tools.filesystem import fs_list, fs_read
from development.tools.git import _ALLOWED_GIT_SUBCMDS, git_diff, git_log, git_status

# ── filesystem ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fs_read_reads_file_in_sandbox(tmp_path):
    """fs_read returns the file's content when the path is inside sandbox."""
    target = tmp_path / "hello.txt"
    target.write_text("hello world", encoding="utf-8")
    result = await fs_read("hello.txt", sandbox_dir=tmp_path)
    assert result["ok"] is True
    assert result["content"] == "hello world"
    assert result["truncated"] is False
    assert result["bytes_read"] == 11


@pytest.mark.asyncio
async def test_fs_read_rejects_parent_traversal(tmp_path):
    """A `../` path resolving outside the sandbox is rejected."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    result = await fs_read("../outside.txt", sandbox_dir=sandbox)
    assert result["ok"] is False
    assert result["error"] == "path_outside_sandbox"


@pytest.mark.asyncio
async def test_fs_read_truncates_at_max_bytes(tmp_path):
    """fs_read caps content length at max_bytes and reports truncated=True."""
    big = tmp_path / "big.txt"
    big.write_text("a" * 1000, encoding="utf-8")
    result = await fs_read("big.txt", sandbox_dir=tmp_path, max_bytes=10)
    assert result["ok"] is True
    assert len(result["content"]) == 10
    assert result["truncated"] is True
    assert result["bytes_read"] == 10


@pytest.mark.asyncio
async def test_fs_list_lists_immediate_children(tmp_path):
    """fs_list returns sorted entries, skipping deeper levels."""
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bb", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "deep.txt").write_text("deep", encoding="utf-8")

    result = await fs_list(".", sandbox_dir=tmp_path)
    assert result["ok"] is True
    names = [e["name"] for e in result["entries"]]
    assert names == ["a.txt", "b.txt", "subdir"]
    # subdir entry has is_dir=True
    sub = next(e for e in result["entries"] if e["name"] == "subdir")
    assert sub["is_dir"] is True


@pytest.mark.asyncio
async def test_fs_list_rejects_outside_sandbox(tmp_path):
    sandbox = tmp_path / "inner"
    sandbox.mkdir()
    result = await fs_list("..", sandbox_dir=sandbox)
    assert result["ok"] is False
    assert result["error"] == "path_outside_sandbox"


# ── git ──────────────────────────────────────────────────────────────


def _git_available() -> bool:
    return shutil.which("git") is not None


def _init_git_repo(repo_dir: Path) -> None:
    """Create a real git repo with one commit so log/diff/status all work."""
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    (repo_dir / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "f.txt"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        env={**env, "PATH": __import__("os").environ.get("PATH", "")},
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        env={**env, "PATH": __import__("os").environ.get("PATH", "")},
    )


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
@pytest.mark.asyncio
async def test_git_status_runs_in_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Modify f.txt to produce a status change.
    (repo / "f.txt").write_text("hello world\n", encoding="utf-8")
    result = await git_status("repo", sandbox_dir=tmp_path)
    assert result["ok"] is True
    assert "f.txt" in result["stdout"]


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
@pytest.mark.asyncio
async def test_git_log_and_git_diff_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    log_out = await git_log("repo", sandbox_dir=tmp_path, limit=5)
    assert log_out["ok"] is True
    assert "initial" in log_out["stdout"]

    # No staged changes yet → empty diff (still ok=True).
    diff_unstaged = await git_diff("repo", sandbox_dir=tmp_path, staged=False)
    assert diff_unstaged["ok"] is True

    diff_staged = await git_diff("repo", sandbox_dir=tmp_path, staged=True)
    assert diff_staged["ok"] is True


@pytest.mark.asyncio
async def test_git_status_rejects_path_outside_sandbox(tmp_path):
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    result = await git_status("..", sandbox_dir=sandbox)
    assert result["ok"] is False
    assert result["error"] == "path_outside_sandbox"


def test_git_allowed_subcmds_is_read_only():
    """The hard-coded list MUST be read-only commands (no commit/push/reset/etc.)."""
    assert _ALLOWED_GIT_SUBCMDS == frozenset({"status", "log", "diff"})
    forbidden = {"push", "commit", "reset", "checkout", "merge", "rebase", "pull"}
    assert _ALLOWED_GIT_SUBCMDS.isdisjoint(forbidden)


# ── exec ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sandboxed_exec_runs_python_print(tmp_path):
    """Spawn `python -c "print('hi')"` and verify stdout."""
    import sys

    result = await sandboxed_exec(
        [sys.executable, "-c", "print('hi')"],
        sandbox_dir=tmp_path,
    )
    assert result["ok"] is True
    assert result["exit_code"] == 0
    # print() produces 'hi' + newline; on Windows the newline may render
    # as either \r\n or \n depending on Python's stream handling. Match both.
    assert result["stdout_tail"].strip() == "hi"
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_sandboxed_exec_rejects_disallowed_binary(tmp_path):
    """A binary not in _ALLOWED_BINS is rejected without spawning."""
    result = await sandboxed_exec(
        ["malicious"],
        sandbox_dir=tmp_path,
    )
    assert result["ok"] is False
    assert result["error"] == "binary_not_allowed"


@pytest.mark.asyncio
async def test_sandboxed_exec_honors_timeout(tmp_path):
    """A 1s sleep with timeout_s=0.1 → timed_out=True, ok=False."""
    import sys

    result = await sandboxed_exec(
        [sys.executable, "-c", "import time; time.sleep(1.0)"],
        sandbox_dir=tmp_path,
        timeout_s=0.2,
    )
    assert result["ok"] is False
    assert result["timed_out"] is True


@pytest.mark.asyncio
async def test_sandboxed_exec_rejects_cwd_outside_sandbox(tmp_path):
    """Using cwd='..' is rejected."""
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    result = await sandboxed_exec(
        ["echo", "hi"],
        sandbox_dir=sandbox,
        cwd="..",
    )
    assert result["ok"] is False
    assert result["error"] == "cwd_outside_sandbox"


def test_allowed_bins_is_minimal():
    """Sanity check on the security boundary: no shell, no eval, no rm."""
    assert _ALLOWED_BINS == frozenset({
        "python", "node", "go", "cargo", "pytest", "vitest", "npm",
        "echo", "ls", "cat",
    })
    # Hardening: things that should NEVER be in here.
    forbidden = {"sh", "bash", "zsh", "cmd", "powershell", "rm", "del",
                 "curl", "wget", "ssh", "scp", "git", "sudo"}
    assert _ALLOWED_BINS.isdisjoint(forbidden)


# ── catalog shape ────────────────────────────────────────────────────


def test_tool_catalog_has_correct_openai_shape():
    """TOOL_CATALOG is a list of OpenAI-format dicts with the right keys."""
    assert isinstance(TOOL_CATALOG, list)
    assert len(TOOL_CATALOG) >= 6  # 7 tools registered

    names = set()
    for entry in TOOL_CATALOG:
        assert isinstance(entry, dict)
        assert entry.get("type") == "function"
        fn = entry.get("function")
        assert isinstance(fn, dict)
        assert "name" in fn and isinstance(fn["name"], str)
        assert "description" in fn
        assert "parameters" in fn
        params = fn["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params
        names.add(fn["name"])

    expected = {
        "fs_read", "fs_list", "git_status", "git_log", "git_diff",
        "sandboxed_exec",
    }
    assert expected.issubset(names)


def test_tool_dispatch_has_all_catalog_names():
    """Every name in TOOL_CATALOG resolves in TOOL_DISPATCH."""
    for entry in TOOL_CATALOG:
        name = entry["function"]["name"]
        assert name in TOOL_DISPATCH, f"{name} missing from TOOL_DISPATCH"


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error_envelope(tmp_path):
    """dispatch_tool_call surfaces unknown_tool without raising."""
    result = await dispatch_tool_call("nope_not_a_tool", {}, tmp_path)
    assert result == {"ok": False, "error": "unknown_tool", "tool": "nope_not_a_tool"}


@pytest.mark.asyncio
async def test_dispatch_bad_arguments_returns_error_envelope(tmp_path):
    """A TypeError from kwargs mismatch comes back as bad_arguments."""
    # fs_read requires `path`; passing an unknown kwarg surfaces as
    # bad_arguments rather than crashing the tool loop.
    result = await dispatch_tool_call(
        "fs_read", {"not_a_real_arg": "x"}, tmp_path
    )
    assert result["ok"] is False
    assert result["error"] == "bad_arguments"


def test_max_tool_calls_default_is_five():
    assert MAX_TOOL_CALLS_PER_LAYER == 5
