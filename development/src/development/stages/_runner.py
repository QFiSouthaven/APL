"""Per-layer test-runner detection and execution.

Owned by :mod:`development.stages.tester`. Spawns whichever runner the
materialized workspace looks like it wants (pytest for Python, vitest /
jest for JS, shellcheck for shell), captures pass/fail counts from
stdout, and returns a uniform :class:`RunnerResult` regardless of which
runner ran.

Sandboxing model: callers (the Tester stage) hand us a ``workspace_dir``
that lives under :func:`tempfile.gettempdir`. We never read or write
anywhere else; subprocesses are spawned with ``cwd=workspace_dir`` and a
copy of the parent ``env`` (we don't strip — pytest/node need ``PATH``
to find their own dependencies).

Failure modes the caller can rely on:

* Runner binary not on PATH → ``detect_runner`` returns ``None`` and
  ``run_tests`` returns ``status='runner_unavailable'``.
* Subprocess hangs past ``timeout_s`` → process tree killed, status
  ``'timeout'``.
* Subprocess crashes (non-zero exit not corresponding to test failures
  we can parse, e.g. an import error) → status ``'errored'``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("development.stages.runner")


_TAIL_CHARS = 1500
_PYTEST_SUMMARY_RE = re.compile(
    r"=+\s*"
    r"(?:(?P<failed>\d+)\s+failed[\s,]*)?"
    r"(?:(?P<passed>\d+)\s+passed[\s,]*)?"
    r".*?=+",
    re.IGNORECASE,
)
# vitest/jest both emit a "Tests: N passed, M failed" summary line.
_JS_SUMMARY_RE = re.compile(
    r"Tests?:?\s+"
    r"(?:(?P<failed>\d+)\s+failed[\s,]*)?"
    r"(?:(?P<passed>\d+)\s+passed[\s,]*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunnerResult:
    """Uniform result envelope for any test runner.

    ``num_passed`` / ``num_failed`` are ``-1`` when the output couldn't
    be parsed (rare — but better than silently reporting 0 of each).
    """

    status: str  # 'passed' | 'failed' | 'errored' | 'runner_unavailable' | 'timeout'
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    num_passed: int
    num_failed: int


# ── detection ───────────────────────────────────────────────────────


def detect_runner(
    workspace_dir: Path,
    layer_name: str,
    files: dict[str, str],
) -> str | None:
    """Pick a runner for this layer's workspace.

    Heuristic table (first match wins):

      * any ``test_*.py`` or ``*_test.py`` file → ``'pytest'`` (if
        pytest is importable; otherwise None — we only check importable
        for pytest because ``shutil.which('pytest')`` is unreliable on
        Windows where pytest is often only installed inside a venv).
      * ``package.json`` with ``vitest`` in dependencies/devDependencies →
        ``'vitest'`` (if ``npx`` is on PATH).
      * ``package.json`` with ``jest`` → ``'jest'`` (if ``npx`` is on PATH).
      * any ``*.sh`` file and ``shellcheck`` is on PATH → ``'shellcheck'``.
      * else → ``None``.
    """
    if not files:
        return None

    paths = list(files.keys())

    # Pytest: any file looking like a pytest test.
    if _has_python_tests(paths):
        if _python_module_available("pytest"):
            return "pytest"
        logger.info(
            "detect_runner: layer %r looks like pytest but the runner is "
            "not importable; reporting unavailable.",
            layer_name,
        )
        return None

    # JS: package.json determines vitest vs jest.
    pkg_json = files.get("package.json")
    if pkg_json is not None and shutil.which("npx") is not None:
        runner = _detect_js_runner(pkg_json)
        if runner is not None:
            return runner

    # Shell scripts → shellcheck (lint, not real exec, but it's the
    # closest stdlib-friendly thing we can do without a bash dependency).
    if _has_shell_files(paths) and shutil.which("shellcheck") is not None:
        return "shellcheck"

    return None


def _has_python_tests(paths: list[str]) -> bool:
    for p in paths:
        name = Path(p).name
        if name.startswith("test_") and name.endswith(".py"):
            return True
        if name.endswith("_test.py"):
            return True
    return False


def _has_shell_files(paths: list[str]) -> bool:
    return any(p.endswith(".sh") for p in paths)


def _python_module_available(name: str) -> bool:
    """``shutil.which`` is unreliable for Python packages; check importability."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _detect_js_runner(pkg_json: str) -> str | None:
    try:
        data = json.loads(pkg_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key) or {}
        if isinstance(section, dict):
            deps.update({str(k): str(v) for k, v in section.items()})
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    return None


# ── execution ───────────────────────────────────────────────────────


async def run_tests(
    workspace_dir: Path,
    runner: str,
    *,
    timeout_s: float = 30.0,
) -> RunnerResult:
    """Spawn ``runner`` in ``workspace_dir`` and return a normalized result.

    On timeout the process tree is killed and ``status='timeout'`` is
    returned. The stdout/stderr tails are still captured (whatever the
    runner wrote before being killed).
    """
    cmd = _build_command(runner)
    if cmd is None:
        return RunnerResult(
            status="runner_unavailable",
            duration_ms=0,
            stdout_tail="",
            stderr_tail=f"Runner {runner!r} is not configured.",
            num_passed=-1,
            num_failed=-1,
        )

    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace_dir),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return RunnerResult(
            status="runner_unavailable",
            duration_ms=int((time.perf_counter() - started) * 1000),
            stdout_tail="",
            stderr_tail=f"{type(exc).__name__}: {exc}",
            num_passed=-1,
            num_failed=-1,
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        # Kill the process tree. On Windows .kill() does an immediate
        # TerminateProcess; on POSIX it sends SIGKILL. Either way the
        # subprocess and its direct children go down.
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
        return RunnerResult(
            status="timeout",
            duration_ms=duration_ms,
            stdout_tail=_tail(stdout_b),
            stderr_tail=_tail(stderr_b),
            num_passed=-1,
            num_failed=-1,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    stdout_s = stdout_b.decode("utf-8", errors="replace")
    stderr_s = stderr_b.decode("utf-8", errors="replace")

    num_passed, num_failed = _parse_counts(runner, stdout_s, stderr_s)
    rc = proc.returncode if proc.returncode is not None else -1

    status = _classify_status(runner, rc, num_passed, num_failed)

    return RunnerResult(
        status=status,
        duration_ms=duration_ms,
        stdout_tail=_tail(stdout_s),
        stderr_tail=_tail(stderr_s),
        num_passed=num_passed,
        num_failed=num_failed,
    )


# ── private helpers ─────────────────────────────────────────────────


def _build_command(runner: str) -> list[str] | None:
    """Translate a runner-name into a CLI command argv.

    For pytest we use ``sys.executable -m pytest`` so the test process
    inherits whatever Python is hosting us — important on Windows where
    a bare ``pytest`` may resolve to a global install with the wrong
    deps.
    """
    if runner == "pytest":
        return [sys.executable, "-m", "pytest", "-v", "--no-header", "-q"]
    if runner == "vitest":
        npx = shutil.which("npx")
        if npx is None:
            return None
        return [npx, "vitest", "run", "--reporter=default"]
    if runner == "jest":
        npx = shutil.which("npx")
        if npx is None:
            return None
        return [npx, "jest", "--ci"]
    if runner == "shellcheck":
        sc = shutil.which("shellcheck")
        if sc is None:
            return None
        return [sc, "*.sh"]
    return None


def _parse_counts(runner: str, stdout: str, stderr: str) -> tuple[int, int]:
    """Pull (passed, failed) counts from runner output. -1 if unparseable."""
    text = stdout + "\n" + stderr
    if runner == "pytest":
        # Look at ALL summary-line matches; the last one (final summary)
        # wins. pytest prints "===== N passed in 0.01s =====" or
        # "===== M failed, N passed in ... =====".
        passed = -1
        failed = -1
        for m in _PYTEST_SUMMARY_RE.finditer(text):
            p = m.group("passed")
            f = m.group("failed")
            if p is not None:
                passed = int(p)
            if f is not None:
                failed = int(f)
        if failed == -1 and passed != -1:
            # "N passed in 0.01s" with no failed segment → failed = 0.
            failed = 0
        return passed, failed
    if runner in ("vitest", "jest"):
        passed = -1
        failed = -1
        for m in _JS_SUMMARY_RE.finditer(text):
            p = m.group("passed")
            f = m.group("failed")
            if p is not None:
                passed = int(p)
            if f is not None:
                failed = int(f)
        if failed == -1 and passed != -1:
            failed = 0
        return passed, failed
    if runner == "shellcheck":
        # shellcheck prints one "In <file> line N:" block per warning;
        # if exit code is 0, treat as 1 passed / 0 failed (the layer
        # has at least one .sh file). Otherwise count blocks.
        return -1, -1
    return -1, -1


def _classify_status(
    runner: str, returncode: int, num_passed: int, num_failed: int
) -> str:
    """Map (returncode, counts) → final status string."""
    # Authoritative signal: parsed counters. If the parser saw failures,
    # status is 'failed' even if rc==0 (rare, but possible if the runner
    # masks failures).
    if num_failed > 0:
        return "failed"
    if returncode == 0:
        return "passed"
    # Non-zero rc with no parsed failures → either tests failed and
    # parsing missed the count (treat as failed if we got *any* signal)
    # or the runner crashed (errored).
    if num_passed >= 0 or num_failed >= 0:
        return "failed"
    # shellcheck: rc!=0 means lint warnings. Treat as failed.
    if runner == "shellcheck" and returncode != 0:
        return "failed"
    return "errored"


def _tail(b: bytes | str, limit: int = _TAIL_CHARS) -> str:
    """Last ``limit`` chars of a stream, decoded if needed."""
    if isinstance(b, bytes):
        s = b.decode("utf-8", errors="replace")
    else:
        s = b
    if len(s) <= limit:
        return s
    return s[-limit:]


__all__ = ["RunnerResult", "detect_runner", "run_tests"]
