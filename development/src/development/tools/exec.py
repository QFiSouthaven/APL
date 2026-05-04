"""Sandboxed subprocess execution for the Coder tool catalog.

The :func:`sandboxed_exec` function spawns a subprocess via
:func:`asyncio.create_subprocess_exec` (no shell) under two hard
constraints:

1. ``argv[0]``'s basename MUST be in :data:`_ALLOWED_BINS`. We compare by
   basename only so ``/usr/bin/python3`` and ``C:\\Python\\python.exe``
   both resolve to ``python``-family. Anything else is rejected
   immediately — no fallback, no warning, no override knob.

2. The resolved cwd MUST be under the provided ``sandbox_dir``. Path
   traversal via ``..`` is rejected.

The allow-list is intentionally small. It covers the common runtimes /
test runners the Coder might want to invoke during a tool-loop and
nothing else. Don't make it permissive — this is THE security boundary
for this tool.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

# THE security boundary. Don't expand without auditing.
_ALLOWED_BINS = frozenset({
    "python",
    "node",
    "go",
    "cargo",
    "pytest",
    "vitest",
    "npm",
    "echo",
    "ls",
    "cat",
})

_TAIL_CHARS = 1500


def _basename_of(arg0: str) -> str:
    """Strip path + extension to get the comparable binary name.

    Examples::

        /usr/bin/python3      → python3
        C:\\Python\\python.exe → python
        node.exe              → node
        pytest                → pytest
    """
    name = Path(arg0).name
    # Strip a trailing .exe (Windows) and a trailing version digit
    # (so python3 → python, python3.12 → python).
    stem = name.rsplit(".", 1)[0] if name.lower().endswith(".exe") else name
    # Trim trailing digits / dots so "python3" → "python", "python3.12" → "python".
    while stem and (stem[-1].isdigit() or stem[-1] == "."):
        stem = stem[:-1]
    return stem.lower()


def _is_allowed_bin(arg0: str) -> bool:
    return _basename_of(arg0) in _ALLOWED_BINS


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


async def sandboxed_exec(
    cmd: list[str],
    *,
    sandbox_dir: Any,
    cwd: str = ".",
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run ``cmd`` in the sandbox; return a normalized envelope.

    Returns ``{ok, exit_code, stdout_tail, stderr_tail, duration_ms,
    timed_out}``. ``ok`` is True iff the process exited 0 within the
    timeout AND argv[0] passed the allow-list check.

    Failure paths (each returns ``ok=False`` with ``error`` set):

    * ``cmd`` empty/non-list           → ``error="bad_cmd"``
    * argv[0] not in _ALLOWED_BINS     → ``error="binary_not_allowed"``
    * cwd resolves outside sandbox     → ``error="cwd_outside_sandbox"``
    * spawn raises FileNotFoundError   → ``error="spawn_failed"``
    """
    if not cmd or not isinstance(cmd, list):
        return {"ok": False, "error": "bad_cmd"}
    if not all(isinstance(part, str) for part in cmd):
        return {"ok": False, "error": "bad_cmd"}

    if not _is_allowed_bin(cmd[0]):
        return {
            "ok": False,
            "error": "binary_not_allowed",
            "binary": cmd[0],
            "allowed": sorted(_ALLOWED_BINS),
        }

    target_cwd = _resolve_inside(sandbox_dir, cwd or ".")
    if target_cwd is None:
        return {"ok": False, "error": "cwd_outside_sandbox"}
    if not target_cwd.is_dir():
        return {"ok": False, "error": "cwd_not_a_directory"}

    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(target_cwd),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return {
            "ok": False,
            "error": "spawn_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=float(timeout_s)
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=2.0
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            stdout_b, stderr_b = b"", b""

    duration_ms = int((time.perf_counter() - started) * 1000)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if len(stdout) > _TAIL_CHARS:
        stdout = stdout[-_TAIL_CHARS:]
    if len(stderr) > _TAIL_CHARS:
        stderr = stderr[-_TAIL_CHARS:]
    rc = proc.returncode if proc.returncode is not None else -1

    return {
        "ok": (not timed_out) and rc == 0,
        "exit_code": rc,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
    }


__all__ = ["sandboxed_exec", "_ALLOWED_BINS"]
