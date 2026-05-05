"""Orchestrated boot for the APL umbrella.

Reads the COMPONENTS table below, spawns each component as a subprocess
(using its own .venv), waits until /api/health returns 200 on every
declared service, then blocks until SIGINT (Ctrl-C). On shutdown,
terminates every child cleanly.

Usage:

    python lab/launch.py                        # boot everything in COMPONENTS
    python lab/launch.py prompt_enhancer        # boot one component
    python lab/launch.py prompt_enhancer round_robin  # specific subset

If a component's /api/health doesn't respond within HEALTH_TIMEOUT, the
launcher reports it and continues with the others. A failed component
does not block boot of healthy peers.

Discovery URLs come from `services.toml` (or DEFAULTS in this module if
the file is absent). Each component must already have its own .venv at
`<component_dir>/.venv/`. Run `python lab/onboarding.py` once per
machine to seed `services.toml`.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

# How each component is launched. Add new entries here when new components
# join the umbrella. The launcher resolves health URL via discovery.
COMPONENTS: dict[str, dict] = {
    "prompt_enhancer": {
        "cwd": REPO_ROOT / "prompt-enhancer",
        # Use the venv's python so we don't depend on PATH activation.
        "command": [".venv/Scripts/python.exe", "-m", "enhancer.cli.main", "ui"],
        "health_path": "/api/health",
    },
    "round_robin": {
        "cwd": REPO_ROOT / "round-robin",
        "command": [".venv/Scripts/python.exe", "app.py"],
        "health_path": "/api/health",
    },
    "development": {
        "cwd": REPO_ROOT / "development",
        "command": [".venv/Scripts/python.exe", "app.py"],
        "health_path": "/api/health",
    },
}

DEFAULT_URLS = {
    "prompt_enhancer": "http://127.0.0.1:8765",
    "round_robin": "http://127.0.0.1:8766",
    "development": "http://127.0.0.1:8767",
}

HEALTH_TIMEOUT = 30.0  # seconds to wait for a single component to come up
HEALTH_INTERVAL = 0.5  # seconds between health probes


def _read_services_toml() -> dict[str, str]:
    """Best-effort read of services.toml; returns {} if missing/malformed."""
    try:
        from platformdirs import user_config_dir

        if sys.version_info >= (3, 11):
            import tomllib  # type: ignore[import-not-found]
        else:  # pragma: no cover
            import tomli as tomllib  # type: ignore[no-redef]

        path = Path(user_config_dir("swarm", appauthor=False)) / "services.toml"
        if not path.exists():
            return {}
        with path.open("rb") as f:
            data = tomllib.load(f)
        services = data.get("services", {})
        return {
            k: v.rstrip("/")
            for k, v in services.items()
            if isinstance(v, str) and v.strip()
        }
    except Exception:
        return {}


def _resolve_url(component: str) -> str:
    """Look up a component's base URL — TOML first, defaults second."""
    return _read_services_toml().get(component) or DEFAULT_URLS.get(component) or ""


def _probe_health(url: str, deadline: float) -> bool:
    """Poll <url>/health (already absolute) until 200 or deadline. Returns True/False."""
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception:
            pass
        time.sleep(HEALTH_INTERVAL)
    return False


def _start_component(name: str) -> tuple[subprocess.Popen | None, str]:
    """Spawn one component; returns (process, full_health_url)."""
    spec = COMPONENTS[name]
    cwd: Path = spec["cwd"]
    command: list[str] = spec["command"]

    if not cwd.exists():
        print(f"[launch] {name}: cwd does not exist: {cwd} — skipping")
        return None, ""

    venv_python = cwd / command[0].replace("/", "\\")
    if not venv_python.exists():
        print(f"[launch] {name}: venv not found at {venv_python} — skipping")
        print(f"[launch]   (run `python -m venv .venv && pip install -e .` in {cwd})")
        return None, ""

    base_url = _resolve_url(name)
    if not base_url:
        print(f"[launch] {name}: no URL in services.toml or defaults — skipping")
        return None, ""

    health_url = base_url.rstrip("/") + spec["health_path"]
    print(f"[launch] {name}: spawning in {cwd}; health = {health_url}")

    # Resolve command[0] (the venv python.exe) to an ABSOLUTE path before
    # spawning. On Windows, subprocess.Popen resolves the executable using
    # the PARENT'S cwd / PATH, not the new `cwd=` we pass. A relative
    # `.venv/Scripts/python.exe` therefore fails with WinError 2 even when
    # cwd is set correctly. Using the resolved venv_python from above sidesteps
    # this entirely and works identically on POSIX.
    abs_command = [str(venv_python)] + list(command[1:])

    try:
        proc = subprocess.Popen(
            abs_command, cwd=str(cwd),
            # Inherit stdout/stderr so the user sees component logs interleaved.
            # On Windows, CREATE_NEW_PROCESS_GROUP lets us send Ctrl-C cleanly.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            ),
        )
    except OSError as exc:
        print(f"[launch] {name}: spawn failed: {exc}")
        print(f"[launch]   command was: {abs_command}")
        return None, ""

    return proc, health_url


def _wait_healthy(name: str, health_url: str) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    if _probe_health(health_url, deadline):
        # Two banners: a structured "[launch]" line (existing contract) and a
        # human-friendly "[ok] <component> at <base-url>" line that matches
        # the umbrella spec wording. Both go to stdout; consumers should not
        # parse either.
        base_url = health_url.rsplit("/api/", 1)[0]
        display_name = name.replace("_", "-")
        print(f"[launch] {name}: HEALTHY ({health_url})")
        print(f"[ok] {display_name} at {base_url}")
        return True
    print(f"[launch] {name}: did NOT become healthy within {HEALTH_TIMEOUT}s")
    return False


def _shutdown(processes: dict[str, subprocess.Popen]) -> None:
    for name, proc in processes.items():
        if proc.poll() is None:
            print(f"[launch] {name}: terminating (pid={proc.pid})")
            try:
                proc.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 10
    for name, proc in processes.items():
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            print(f"[launch] {name}: did not exit cleanly; killing")
            try:
                proc.kill()
            except OSError:
                pass


def _check_config(targets: list[str]) -> int:
    """Dry-run: report each target's resolved cwd, command, and base URL.

    No subprocesses are spawned and no health probes are issued. Returns
    0 if every target's prerequisites (cwd + venv python) exist, 1 if
    any are missing.
    """
    bad = 0
    for name in targets:
        spec = COMPONENTS[name]
        cwd: Path = spec["cwd"]
        cmd: list[str] = spec["command"]
        venv_python = cwd / cmd[0].replace("/", "\\")
        url = _resolve_url(name)
        print(f"[check] {name}:")
        print(f"[check]   cwd        = {cwd} {'(exists)' if cwd.exists() else '(MISSING)'}")
        print(f"[check]   python     = {venv_python} {'(exists)' if venv_python.exists() else '(MISSING)'}")
        print(f"[check]   command    = {' '.join(cmd)}")
        print(f"[check]   base url   = {url or '(unresolved)'}")
        if not cwd.exists() or not venv_python.exists() or not url:
            bad += 1
    if bad:
        print(f"[check] {bad} component(s) not bootable on this machine")
        return 1
    print("[check] all components ready to boot")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Boot the APL umbrella components.")
    parser.add_argument(
        "components", nargs="*",
        help="Names of components to boot (default: all in COMPONENTS).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Validate config and prerequisites without spawning anything.",
    )
    args = parser.parse_args(argv)

    targets = args.components or list(COMPONENTS.keys())
    invalid = [t for t in targets if t not in COMPONENTS]
    if invalid:
        print(f"[launch] unknown components: {invalid}")
        print(f"[launch] valid choices: {list(COMPONENTS.keys())}")
        return 2

    if args.check:
        return _check_config(targets)

    processes: dict[str, subprocess.Popen] = {}
    health_urls: dict[str, str] = {}

    for name in targets:
        proc, health_url = _start_component(name)
        if proc is not None:
            processes[name] = proc
            health_urls[name] = health_url

    if not processes:
        print("[launch] no components booted")
        return 1

    failed: list[str] = []
    for name, url in health_urls.items():
        if not _wait_healthy(name, url):
            failed.append(name)

    if failed:
        print(f"[launch] some components unhealthy: {failed}")
        # Continue; user can debug while healthy peers run.
    print("[launch] all running. Ctrl-C to stop.")

    interrupted = False

    def _on_sigint(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        while not interrupted:
            time.sleep(0.5)
            for name, proc in list(processes.items()):
                if proc.poll() is not None:
                    print(f"[launch] {name}: exited (code={proc.returncode})")
                    processes.pop(name)
            if not processes:
                print("[launch] all components exited; stopping launcher")
                return 0
    finally:
        _shutdown(processes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
