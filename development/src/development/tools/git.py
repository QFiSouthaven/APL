"""Read-only git tools, scoped to a sandbox dir.

Hard-coded allowed subcommand list (``_ALLOWED_GIT_SUBCMDS``). Anything
that mutates — push, commit, reset, checkout, merge, rebase, pull, fetch
in the sense of writing to the working tree, etc. — is unreachable; we
expose dedicated wrapper functions that pin the subcommand argument.

Each call shells out via :func:`asyncio.create_subprocess_exec` with a
10-second timeout. ``cwd`` is constrained to the sandbox dir.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

# Module-level allow-list — the security boundary. Wrappers below must
# only pass these strings as the git subcommand.
_ALLOWED_GIT_SUBCMDS = frozenset({"status", "log", "diff"})

_GIT_TIMEOUT_S = 10.0
_TAIL_CHARS = 4000


def _resolve_inside(sandbox_dir: Any, path: str) -> Path | None:
    if sandbox_dir is None:
        return None
    root = Path(sandbox_dir).resolve()
    p = Path(path) if path else Path(".")
    if p.is_absolute():
        return None
    try:
        resolved = (root / p).resolve()
    except (OSError, ValueError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


async def _run_git(
    repo_dir: Path,
    subcommand: str,
    *args: str,
) -> dict[str, Any]:
    """Spawn ``git <subcommand> <args...>`` with the safety guards.

    ``subcommand`` MUST be in :data:`_ALLOWED_GIT_SUBCMDS`. Wrappers
    (``git_status``/``git_log``/``git_diff``) hardcode it; this check is
    a belt-and-braces safeguard against future refactors.
    """
    if subcommand not in _ALLOWED_GIT_SUBCMDS:
        return {"ok": False, "error": "subcommand_not_allowed", "subcommand": subcommand}
    git_bin = shutil.which("git")
    if git_bin is None:
        return {"ok": False, "error": "git_not_available"}
    try:
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            subcommand,
            *args,
            cwd=str(repo_dir),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return {"ok": False, "error": "spawn_failed", "detail": str(exc)}

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"ok": False, "error": "timeout"}

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if len(stdout) > _TAIL_CHARS:
        stdout = stdout[-_TAIL_CHARS:]
    if len(stderr) > _TAIL_CHARS:
        stderr = stderr[-_TAIL_CHARS:]
    rc = proc.returncode if proc.returncode is not None else -1
    return {
        "ok": rc == 0,
        "exit_code": rc,
        "stdout": stdout,
        "stderr": stderr,
    }


async def git_status(
    repo_dir: str = ".",
    *,
    sandbox_dir: Any,
) -> dict[str, Any]:
    """Run ``git status --porcelain`` in ``repo_dir`` (under sandbox)."""
    target = _resolve_inside(sandbox_dir, repo_dir)
    if target is None:
        return {"ok": False, "error": "path_outside_sandbox"}
    if not target.is_dir():
        return {"ok": False, "error": "not_a_directory"}
    return await _run_git(target, "status", "--porcelain")


async def git_log(
    repo_dir: str = ".",
    *,
    sandbox_dir: Any,
    limit: int = 10,
) -> dict[str, Any]:
    """Run ``git log -n <limit> --oneline`` in ``repo_dir``."""
    target = _resolve_inside(sandbox_dir, repo_dir)
    if target is None:
        return {"ok": False, "error": "path_outside_sandbox"}
    if not target.is_dir():
        return {"ok": False, "error": "not_a_directory"}
    n = max(1, int(limit))
    return await _run_git(target, "log", f"-n{n}", "--oneline")


async def git_diff(
    repo_dir: str = ".",
    *,
    sandbox_dir: Any,
    staged: bool = False,
) -> dict[str, Any]:
    """Run ``git diff`` (or ``git diff --staged``) in ``repo_dir``."""
    target = _resolve_inside(sandbox_dir, repo_dir)
    if target is None:
        return {"ok": False, "error": "path_outside_sandbox"}
    if not target.is_dir():
        return {"ok": False, "error": "not_a_directory"}
    if staged:
        return await _run_git(target, "diff", "--staged")
    return await _run_git(target, "diff")


__all__ = ["git_status", "git_log", "git_diff", "_ALLOWED_GIT_SUBCMDS"]
