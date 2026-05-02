"""CLI expansions: ``batch``, ``compare``, ``export``.

These are wired into the main typer app at ``cli.main`` via
:func:`register`. They live in a separate module to keep ``main.py``
readable.
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pyperclip
import typer
from rich.console import Console
from rich.table import Table

from ..config import db_path, jsonl_log_path, load
from ..core.events import EventType
from ..core.pipeline import PipelineOptions, build_resume_state, run_pipeline
from ..llm.registry import get_provider
from ..persistence import runs as runs_module


async def _run_with_auto_resume(
    prompt: str,
    *,
    provider, model: str, opts: PipelineOptions,
    on_event, request_timeout: float, idle_timeout: float,
):
    """Run the pipeline; if it pauses for disambiguation, auto-resume with
    empty answers (the equivalent of ``--skip-clarify`` for batch/compare).

    Avoids ``compare`` and ``batch`` silently returning ``P4_DEFAULTS`` when
    the user's prompt is vague enough to trigger Pass 2's weakness-field
    threshold.
    """
    pending: dict[str, dict] = {}
    captured: dict[str, object] = {}

    async def _wrapped_on_event(event_type, **payload):
        name = (
            event_type.value if hasattr(event_type, "value") else str(event_type)
        )
        if name == EventType.AGENT_DISAMBIGUATE.value:
            captured["disambig_id"] = payload.get("disambig_id")
            captured["questions"] = payload.get("questions") or []
        if on_event is not None:
            await on_event(event_type, **payload)

    result = await run_pipeline(
        prompt,
        provider=provider, model=model, opts=opts,
        on_event=_wrapped_on_event,
        request_timeout=request_timeout, idle_timeout=idle_timeout,
        pending_disambig=pending,
    )
    if result.extras and result.extras.get("paused") and captured.get("disambig_id"):
        snapshot = pending.get(captured["disambig_id"])
        if snapshot is not None:
            resume_state = build_resume_state(snapshot, {})
            result = await run_pipeline(
                snapshot["prompt"],
                provider=provider, model=model,
                opts=PipelineOptions(
                    scorer_model=snapshot.get("scorer_model"),
                    persona_mode=snapshot.get("persona_mode", False),
                    magnitude_mode=snapshot.get("magnitude_mode", False),
                    sot_mode=snapshot.get("sot_mode", False),
                    session_id=snapshot.get("session_id"),
                    temperature=opts.temperature,
                    max_tokens_scale=opts.max_tokens_scale,
                    resume_state=resume_state,
                ),
                on_event=_wrapped_on_event,
                request_timeout=request_timeout, idle_timeout=idle_timeout,
            )
    return result

console = Console()


def register(app: typer.Typer) -> None:
    """Attach the extra commands to an existing typer app."""
    app.command()(batch)
    app.command()(compare)
    app.command()(export)


# ── batch ────────────────────────────────────────────────────────────

def batch(
    file: Path = typer.Argument(..., help="JSON / JSONL file with prompts"),
    out: Path = typer.Option(Path("results.csv"), "--out", "-o",
                             help="CSV output path"),
    model: str = typer.Option("", help="Override default model"),
    temperature: float = typer.Option(0.7, "--temperature", "-t"),
    max_tokens_scale: float = typer.Option(1.0, "--tokens"),
    quiet: bool = typer.Option(True, "--quiet/--verbose"),
) -> None:
    """Run the pipeline over a JSON / JSONL list of prompts; write CSV.

    Input forms accepted:
    * ``[ "prompt 1", "prompt 2", ... ]``       (JSON array)
    * ``[ {"prompt": "...", "session_id": "..."} ]``  (objects)
    * one prompt or one JSON object per line    (JSONL)
    """
    settings = load()
    provider = get_provider(settings)
    chosen_model = model or settings.default_model

    prompts = _read_prompts(file)
    if not prompts:
        console.print("[red]No prompts found in input.[/red]")
        raise typer.Exit(code=2)

    rows: list[dict] = []

    async def _on_event(*_, **__):  # pragma: no cover — quiet by default
        return

    async def _go() -> None:
        for i, item in enumerate(prompts, 1):
            prompt_text = item if isinstance(item, str) else item.get("prompt", "")
            if not prompt_text:
                continue
            if not quiet:
                console.print(f"[dim]{i}/{len(prompts)}[/dim] {prompt_text[:60]}")
            result = await _run_with_auto_resume(
                prompt_text,
                provider=provider, model=chosen_model,
                opts=PipelineOptions(
                    temperature=temperature,
                    max_tokens_scale=max_tokens_scale,
                    session_id=item.get("session_id") if isinstance(item, dict) else None,
                ),
                on_event=_on_event,
                request_timeout=settings.request_timeout,
                idle_timeout=settings.idle_timeout,
            )
            record = result.extras.get("_record") if result.extras else None
            if record is not None:
                runs_module.save(record, db_path(), jsonl_log_path())
            rows.append({
                "prompt": prompt_text,
                "enhanced": result.result,
                "task_type": result.task_type,
                "technique": result.technique,
                "specificity": result.scores.get("specificity"),
                "constraints": result.scores.get("constraints"),
                "actionability": result.scores.get("actionability"),
                "improvement": result.scores.get("improvement"),
                "scores_fallback": result.scores_fallback,
                "model": result.model,
                "run_id": result.run_id,
            })

    asyncio.run(_go())

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["prompt"])
        writer.writeheader()
        writer.writerows(rows)
    console.print(f"[green]Wrote {len(rows)} rows → {out}[/green]")


def _read_prompts(file: Path) -> list:
    text = file.read_text(encoding="utf-8")
    text_stripped = text.strip()
    if not text_stripped:
        return []
    # JSON array form
    if text_stripped.startswith("["):
        return json.loads(text_stripped)
    # JSONL form
    out: list = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append(line)
    return out


# ── compare ──────────────────────────────────────────────────────────

def compare(
    prompt: str = typer.Argument(..., help="Prompt to enhance with both models"),
    model_a: str = typer.Option(..., "--a", help="First model"),
    model_b: str = typer.Option(..., "--b", help="Second model"),
    temperature: float = typer.Option(0.7, "-t", "--temperature"),
) -> None:
    """Run the pipeline twice with different models; print a score diff.

    **Always serial** — never parallel. Single-instance LM Studio /
    LM Link backends queue concurrent calls and time out (this is the
    very lesson the source monolith taught us; see
    ``docs/EXTRACTION_GOTCHAS.md`` §3).
    """
    settings = load()
    provider = get_provider(settings)

    async def _on_event(*_, **__):
        return

    async def _go() -> tuple[dict, dict]:
        # Both runs auto-resume on disambiguation pause so the comparison
        # actually scores both rewrites instead of silently falling back to
        # P4_DEFAULTS for a vague prompt.
        a = await _run_with_auto_resume(
            prompt, provider=provider, model=model_a,
            opts=PipelineOptions(temperature=temperature),
            on_event=_on_event,
            request_timeout=settings.request_timeout,
            idle_timeout=settings.idle_timeout,
        )
        b = await _run_with_auto_resume(
            prompt, provider=provider, model=model_b,
            opts=PipelineOptions(temperature=temperature),
            on_event=_on_event,
            request_timeout=settings.request_timeout,
            idle_timeout=settings.idle_timeout,
        )
        if a.extras and a.extras.get("_record"):
            runs_module.save(a.extras["_record"], db_path(), jsonl_log_path())
        if b.extras and b.extras.get("_record"):
            runs_module.save(b.extras["_record"], db_path(), jsonl_log_path())
        return ({"model": model_a, "scores": a.scores, "result": a.result, "run_id": a.run_id},
                {"model": model_b, "scores": b.scores, "result": b.result, "run_id": b.run_id})

    a, b = asyncio.run(_go())

    table = Table(title=f"Compare: {model_a}  vs  {model_b}")
    table.add_column("metric")
    table.add_column(model_a, justify="right")
    table.add_column(model_b, justify="right")
    table.add_column("Δ", justify="right")

    for key in ("specificity", "constraints", "actionability", "improvement"):
        va, vb = a["scores"].get(key, 0), b["scores"].get(key, 0)
        delta = vb - va
        marker = "[green]+" if delta > 0 else "[red]" if delta < 0 else "[dim]"
        table.add_row(key, str(va), str(vb), f"{marker}{delta:+}[/]" if delta else "[dim]0[/]")

    console.print(table)
    console.print(f"\n[bold]{model_a}[/bold] run_id: {a['run_id']}")
    console.print(f"[bold]{model_b}[/bold] run_id: {b['run_id']}")


# ── export ───────────────────────────────────────────────────────────

def export(
    run_id: str = typer.Argument(..., help="Run id from `enhancer history`"),
    fmt: str = typer.Option("clipboard", "--format", "-f",
                            help="clipboard | md | curl | json"),
    out: Path = typer.Option(None, "--out", "-o",
                             help="Optional file path; default stdout"),
) -> None:
    """Export an enhanced prompt in various formats."""
    record = runs_module.get_run(db_path(), run_id)
    if not record:
        console.print(f"[red]Run {run_id} not found[/red]")
        raise typer.Exit(code=1)

    body: str = ""
    if fmt == "clipboard":
        pyperclip.copy(record["enhanced_prompt"])
        console.print("[green]Copied enhanced prompt to clipboard.[/green]")
        return
    if fmt == "md":
        body = (
            f"# Run {record['id']}\n\n"
            f"**Original:**\n\n```\n{record['prompt']}\n```\n\n"
            f"**Enhanced:**\n\n```\n{record['enhanced_prompt']}\n```\n\n"
            f"_task_type_: {record.get('task_type')}, "
            f"_technique_: {record.get('technique')}, "
            f"_improvement_: {record.get('improvement')}%\n"
        )
    elif fmt == "json":
        body = json.dumps(record, indent=2, default=str)
    elif fmt == "curl":
        # OpenAI-compatible inference call template — caller fills in URL/key.
        body = (
            "curl -sS http://127.0.0.1:1234/v1/chat/completions \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -d '" + json.dumps({
                "model": record.get("model") or "your-model",
                "messages": [{"role": "user", "content": record["enhanced_prompt"]}],
                "stream": False,
            }) + "'\n"
        )
    else:
        console.print(f"[red]Unknown format: {fmt}[/red]")
        raise typer.Exit(code=2)

    if out is None:
        console.print(body)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        console.print(f"[green]Wrote → {out}[/green]")
