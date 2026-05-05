"""typer CLI entry point — ``enhancer`` from pyproject.toml scripts.

v1 surface:

* ``enhancer enhance "prompt"`` — run the 4-pass pipeline, stream output.
* ``enhancer history`` — list recent runs from SQLite.
* ``enhancer ui`` — launch the NiceGUI Desktop Studio.
* ``enhancer models`` — list models the configured provider exposes.
* ``enhancer version`` — print the package version.

Phase 4 fully fleshes this out with batch / compare / export.
"""

from __future__ import annotations

import asyncio
import sys

import typer
from rich.console import Console

# On Windows, stdout's default code page (cp1252 / cp437) can't render
# common Unicode characters that LLMs emit (e.g. non-breaking hyphen
# ‑, em dashes, smart quotes). Reconfigure stdout to UTF-8 with
# replacement so the CLI never crashes on a stray glyph.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from .. import __version__
from ..config import db_path, jsonl_log_path, load
from ..core.events import EventType
from ..core.pipeline import PipelineOptions, build_resume_state, run_pipeline
from ..llm.host_picker import apply_host_pick, parse_hosts
from ..llm.lms_discovery import ModelLoadUnavailableError, ensure_model_loaded
from ..llm.registry import get_provider
from ..persistence import runs as runs_module

# Env var consulted by the global typer callback when ``--lms-hosts`` is
# not passed. Documented in the help string for the flag.
LMS_HOSTS_ENV = "ENHANCER_LMS_HOSTS"

app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="Local Desktop Studio for multi-pass AI prompt enhancement.",
)
console = Console()


@app.callback()
def _global_options(
    lms_hosts: str = typer.Option(
        "",
        "--lms-hosts",
        envvar=LMS_HOSTS_ENV,
        help=(
            "Comma-separated LM Studio base URLs to probe at startup. "
            "First host with a chat-capable model already loaded becomes "
            "the active inference target. Falls back silently to the "
            "configured default if no host responds. Also reads "
            f"{LMS_HOSTS_ENV}."
        ),
    ),
) -> None:
    """Global CLI options. Multi-host LAN-discovery happens here so it
    runs before any subcommand pre-flights ``ensure_model_loaded``.
    """
    hosts = parse_hosts(lms_hosts)
    if not hosts:
        return
    chosen_host, chosen_model = asyncio.run(apply_host_pick(hosts))
    if chosen_host:
        console.print(
            f"[dim]LM Studio host picker → {chosen_host} (model {chosen_model})[/dim]"
        )
    else:
        console.print(
            "[yellow]LM Studio host picker: no host in --lms-hosts responded "
            "with a loaded chat model; using configured default.[/yellow]"
        )


@app.command()
def version() -> None:
    """Print the package version and exit."""
    console.print(f"prompt-enhancer {__version__}")


@app.command()
def models() -> None:
    """List models the configured provider exposes."""
    settings = load()
    provider = get_provider(settings)

    async def _go() -> list[str]:
        return await provider.list_models()

    out = asyncio.run(_go())
    if not out:
        console.print("[yellow]No models found — is LM Studio running?[/yellow]")
        raise typer.Exit(code=1)
    for m in out:
        console.print(m)


@app.command()
def enhance(
    prompt: str = typer.Argument(..., help="The prompt to enhance"),
    model: str = typer.Option("", help="Override the configured default model"),
    scorer_model: str = typer.Option("", "--scorer", help="Pass 4 scorer model"),
    temperature: float = typer.Option(0.7, "--temperature", "-t",
                                      help="Sampling temperature 0.0–2.0"),
    max_tokens_scale: float = typer.Option(1.0, "--tokens", help="Max-tokens scale 0.3–3.0"),
    persona: bool = typer.Option(False, "--persona", help="Enable persona mode"),
    magnitude: bool = typer.Option(False, "--magnitude", help="Run Magnitude blueprint"),
    sot: bool = typer.Option(False, "--sot", help="Run Skeleton-of-Thought"),
    skip_clarify: bool = typer.Option(
        False, "--skip-clarify", "-y",
        help="Skip the interactive disambiguation pause; proceed straight to rewrite",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the final result"),
) -> None:
    """Run the 4-pass enhancer on PROMPT and print the result.

    If Pass 2 detects a vague prompt (≥3 weakness fields), the pipeline
    pauses and asks 2-3 multiple-choice clarification questions. Use
    --skip-clarify to disable the pause and proceed directly with no
    extra context. Use --quiet to suppress streaming output and print
    only the final enhanced prompt.
    """
    settings = load()
    provider = get_provider(settings)
    chosen_model = model or settings.default_model

    # Ensure LM Studio has a chat model loaded — auto-load via `lms` CLI
    # if necessary. Surfaces a clear error instead of silent empty
    # completions when the user forgot to load one. Only applied for
    # the LM Studio provider (the only backend with a load CLI today).
    if getattr(provider, "name", "") == "lmstudio":
        try:
            chosen_model = asyncio.run(
                ensure_model_loaded(preferred=chosen_model or None)
            )
        except ModelLoadUnavailableError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from None
    elif not chosen_model:
        async def _first() -> str:
            mods = await provider.list_models()
            return mods[0] if mods else ""
        chosen_model = asyncio.run(_first())
        if not chosen_model:
            console.print("[red]No model specified and provider returned no list. "
                          "Set ENHANCER_DEFAULT_MODEL or pass --model.[/red]")
            raise typer.Exit(code=2)

    final_result: dict[str, str] = {"text": ""}
    captured_disambig: dict[str, dict] = {}  # filled by on_event when paused

    async def _on_event(event_type, **payload):
        name = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if name == EventType.AGENT_DISAMBIGUATE.value:
            # Always capture, even in --quiet mode — needed for the resume path.
            captured_disambig["disambig_id"] = payload["disambig_id"]
            captured_disambig["questions"] = payload["questions"]
            return
        if quiet:
            return
        if name == EventType.AGENT_PASS_CHUNK.value and payload.get("pass_number") == 3:
            console.print(payload["token"], end="")
        elif name == EventType.AGENT_PASS_START.value:
            console.print(f"\n[bold cyan]── {payload['pass_name']} ──[/bold cyan]")
        elif name == EventType.AGENT_DONE.value:
            console.print()
        elif name == EventType.AGENT_ERROR.value:
            console.print(f"[red]ERROR ({payload.get('step')}): "
                          f"{payload.get('error')}[/red]")

    def _ask_clarifications(questions: list[dict]) -> dict[str, str]:
        """Display each Q with lettered options; return {Q1: answer-text}."""
        console.print(
            "\n[bold yellow]── Pipeline paused for clarification ──[/bold yellow]"
        )
        console.print(
            "[dim]Pass 2 detected several gaps in the prompt. Answer below "
            "(letter or free-text); the pipeline will resume with your "
            "clarifications injected into Pass 3.[/dim]\n"
        )
        answers: dict[str, str] = {}
        for i, q in enumerate(questions):
            qid = f"Q{i + 1}"
            console.print(f"[bold]{qid}: {q['question']}[/bold]")
            options = q.get("options", [])
            for j, opt in enumerate(options):
                console.print(f"  {chr(65 + j)}) {opt}")
            raw = typer.prompt(f"{qid} answer (A/B/C or free-text)", default="").strip()
            if not raw:
                continue
            # Map A/B/C to option text; otherwise treat as free-text.
            up = raw.upper()
            if len(up) == 1 and "A" <= up <= chr(64 + len(options)):
                answers[qid] = options[ord(up) - 65]
            else:
                answers[qid] = raw
        return answers

    async def _go() -> None:
        pending_disambig: dict[str, dict] = {}
        result = await run_pipeline(
            prompt,
            provider=provider, model=chosen_model,
            opts=PipelineOptions(
                scorer_model=scorer_model or None,
                magnitude_mode=magnitude, persona_mode=persona, sot_mode=sot,
                temperature=temperature, max_tokens_scale=max_tokens_scale,
            ),
            on_event=_on_event,
            request_timeout=settings.request_timeout,
            idle_timeout=settings.idle_timeout,
            pending_disambig=pending_disambig,
        )

        # Disambiguation pause path — collect answers, build resume_state, retry.
        if (
            result.extras
            and result.extras.get("paused")
            and captured_disambig.get("disambig_id")
        ):
            disambig_id = captured_disambig["disambig_id"]
            snapshot = pending_disambig.get(disambig_id)
            if snapshot is None:
                console.print("[red]Internal error: disambig snapshot missing.[/red]")
                return

            if skip_clarify:
                answers: dict[str, str] = {}
                if not quiet:
                    console.print(
                        "[dim]--skip-clarify set; resuming with no clarifications.[/dim]"
                    )
            else:
                answers = _ask_clarifications(captured_disambig["questions"])

            resume_state = build_resume_state(snapshot, answers)
            if not quiet:
                console.print(
                    "\n[bold green]── Resuming pipeline ──[/bold green]"
                )
            result = await run_pipeline(
                snapshot["prompt"],
                provider=provider, model=chosen_model,
                opts=PipelineOptions(
                    scorer_model=snapshot.get("scorer_model"),
                    magnitude_mode=snapshot.get("magnitude_mode", False),
                    persona_mode=snapshot.get("persona_mode", False),
                    sot_mode=snapshot.get("sot_mode", False),
                    session_id=snapshot.get("session_id"),
                    temperature=temperature,
                    max_tokens_scale=max_tokens_scale,
                    resume_state=resume_state,
                ),
                on_event=_on_event,
                request_timeout=settings.request_timeout,
                idle_timeout=settings.idle_timeout,
            )

        final_result["text"] = result.result
        record = result.extras.get("_record") if result.extras else None
        if record is not None:
            runs_module.save(record, db_path(), jsonl_log_path())

    asyncio.run(_go())

    if quiet:
        console.print(final_result["text"])


@app.command()
def history(
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows"),
    task_type: str = typer.Option("", help="Filter by task type"),
    min_improvement: int = typer.Option(0, help="Filter improvement ≥ N"),
) -> None:
    """List recent pipeline runs from SQLite."""
    rows = runs_module.list_recent(
        db_path(), limit=limit,
        task_type=task_type or None,
        min_improvement=min_improvement or None,
    )
    if not rows:
        console.print("[yellow]No runs yet — try `enhancer enhance \"...\"`[/yellow]")
        return
    for r in rows:
        score = r.get("improvement")
        score_str = f"+{score}%" if score is not None else "—"
        console.print(
            f"[dim]{r['ts']}[/dim] "
            f"[bold]{r.get('task_type', '?')}[/bold] "
            f"[cyan]{score_str}[/cyan] "
            f"{r['prompt'][:60]}"
        )


@app.command()
def ui() -> None:
    """Launch the NiceGUI Desktop Studio."""
    try:
        from ..ui.app import run as run_ui  # noqa: F401
    except ImportError as exc:
        console.print(
            f"[red]UI not installed: {exc}[/red]\n"
            "Install with: [bold]pip install prompt-enhancer[ui][/bold]"
        )
        raise typer.Exit(code=2) from None
    run_ui()


# Wire batch / compare / export commands.
from .extras import register as _register_extras  # noqa: E402

_register_extras(app)

# Wire the `services` sub-app (show / init / path).
from ._services import register as _register_services  # noqa: E402

_register_services(app)


if __name__ == "__main__":
    app()
