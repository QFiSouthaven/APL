"""``enhancer services`` subcommands — make ``services.toml`` discoverable.

The discovery layer (:mod:`enhancer.api.discovery`) reads
``%APPDATA%\\swarm\\services.toml`` if present and falls back to
``DEFAULTS`` otherwise. Until now no CLI command created the file or
exposed its location, so users had no idiomatic way to override peer
URLs. These three subcommands close that gap:

* ``enhancer services show`` — print the resolved peer table (defaults
  merged with ``services.toml``), where the file lives, and whether it
  exists.
* ``enhancer services init`` — write a friendly starter ``services.toml``
  at :func:`enhancer.api.discovery.services_path`. Refuses to clobber
  unless ``--force`` is passed.
* ``enhancer services path`` — print just the absolute path of
  ``services.toml`` (handy for ``start "" $(enhancer services path)``).

The starter file contains the **DEFAULTS table inlined** (so the file
parses to a working ``[services]`` block even if the user changes
nothing), plus a header comment block explaining the schema, the
precedence order, and pointing to ``docs/SERVICES.md``. ``tomli_w``
performs the actual TOML write — it was promoted to a runtime
dependency in commit 20112ff.
"""

from __future__ import annotations

import typer
from rich.console import Console

from ..api import discovery

console = Console()

# Plain ASCII header — Windows cp1252 stdout is fine, no exotic glyphs.
_HEADER = """\
# services.toml — APL inter-product discovery
#
# This file lets the four-product loop (Prompt Enhancer, Round Robin,
# Interpreter, Loop Driver) find each other. Each product reads this
# file on demand; defaults to localhost loopback ports if absent.
#
# Precedence:
#   1. This file's [services] table
#   2. Built-in DEFAULTS in enhancer.api.discovery.DEFAULTS
#
# Known peer names (snake_case — round_robin, NOT round-robin):
#   prompt_enhancer  — the prompt enhancer Studio + REST API
#   round_robin      — the round-robin sibling
#   development      — interpreter / dev sandbox peer
#
# To override a peer, edit the value below or uncomment one of the
# example LAN overrides at the bottom. Re-run `enhancer services show`
# to confirm the file parses and the override is picked up.
#
# Reference: docs/SERVICES.md in the prompt-enhancer repo.

"""

_FOOTER = """

# ── Example LAN overrides (uncomment + edit to use) ──────────────────
# [services]
# prompt_enhancer = "http://192.168.1.50:8765"
# round_robin     = "http://192.168.1.51:8766"
# development     = "http://192.168.1.52:8767"
"""


def _render_starter_toml() -> str:
    """Render the starter ``services.toml`` body.

    Strategy: inline the ``DEFAULTS`` table verbatim (so the file is
    valid and yields a populated ``[services]`` block out of the box),
    and append a commented-out LAN-override example. We do not use
    ``tomli_w.dumps`` for the active section because we want the
    surrounding header comments preserved verbatim — ``tomli_w``
    cannot emit comments.
    """
    # Defensive: stable-sort default keys for deterministic output.
    # tomli_w is imported lazily so a minimal install (no `pip install
    # -e .[dev,ui]`) doesn't trip on import; it's a runtime dep but the
    # lazy import mirrors enhancer.config's pattern from commit 20112ff.
    import tomli_w  # noqa: F401  — verifies the dep is present

    body = "[services]\n"
    for key in sorted(discovery.DEFAULTS):
        body += f'{key} = "{discovery.DEFAULTS[key]}"\n'
    return _HEADER + body + _FOOTER


# ── typer sub-app ────────────────────────────────────────────────────

services_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and bootstrap the cross-sibling services.toml file.",
)


@services_app.command("show")
def show() -> None:
    """Print the resolved peer table and where ``services.toml`` lives."""
    path = discovery.services_path()
    exists = path.exists()
    peers = discovery.get_all_peers()

    console.print(f"[bold]services.toml[/bold]: {path}")
    console.print(
        f"[dim]exists:[/dim] {'yes' if exists else 'no — using DEFAULTS only'}"
    )
    console.print()
    console.print("[bold]Resolved peers[/bold] (file > DEFAULTS):")
    # Stable order: known peers first, then any user-defined extras.
    seen: set[str] = set()
    for key in sorted(discovery.DEFAULTS):
        seen.add(key)
        console.print(f"  {key:<18} {peers.get(key, '')}")
    extras = sorted(k for k in peers if k not in seen)
    for key in extras:
        console.print(f"  {key:<18} {peers[key]}  [dim](custom)[/dim]")


@services_app.command("init")
def init(
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Overwrite an existing services.toml.",
    ),
) -> None:
    """Write a starter ``services.toml`` at the discovery path."""
    path = discovery.services_path()
    if path.exists() and not force:
        console.print(
            f"[yellow]services.toml already exists at {path}[/yellow]"
        )
        console.print(
            "Use [bold]enhancer services init --force[/bold] to overwrite."
        )
        raise typer.Exit(code=1)

    # Parent dir may not exist on a fresh user profile (platformdirs
    # never auto-creates). mkdir(parents=True, exist_ok=True) is safe
    # both for first-run and when --force re-runs against a dir that
    # already exists.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_starter_toml(), encoding="utf-8")
    action = "Overwrote" if force else "Wrote"
    console.print(f"[green]{action} starter services.toml -> {path}[/green]")


@services_app.command("path")
def path_cmd() -> None:
    """Print just the absolute path of ``services.toml``."""
    # Intentionally bare print() — no rich markup — so the output
    # is clean for shell substitution: `start "" $(enhancer services path)`.
    print(str(discovery.services_path()))


def register(app: typer.Typer) -> None:
    """Attach the ``services`` sub-app to the parent typer app."""
    app.add_typer(services_app, name="services")
