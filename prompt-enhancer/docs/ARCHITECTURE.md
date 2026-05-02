# Architecture

> The standalone Prompt Enhancer is a single-process Python application
> with a transport-agnostic core and pluggable backends. This doc maps
> the moving parts.

## Layered view

```
┌────────────── transports ──────────────┐
│ CLI (typer)        Desktop Studio      │
│ enhancer enhance   NiceGUI @ :8765     │
└─────┬────────────────────┬─────────────┘
      │                    │
      ▼                    ▼
   ┌─── core ───────────────────────────┐
   │ pipeline.run_pipeline(             │
   │     prompt, opts, on_event=cb,     │
   │     provider=...,                  │
   │ )                                  │
   └─────┬──────────────────┬───────────┘
         │                  │
         ▼                  ▼
   ┌── llm ──────┐   ┌── persistence ──┐
   │ ChatProvider│   │ runs / sessions │
   │   ABC       │   │ scores / temp.  │
   │             │   │ SQLite + JSONL  │
   │ LMStudio,   │   │ dual-writer     │
   │ Ollama, ... │   │                 │
   └─────────────┘   └─────────────────┘
```

The **only coupling point** between the core pipeline and any
transport is `on_event` — an async callback `(EventType, **payload) →
None`. The CLI wires it to Rich console output; the Studio wires it to
NiceGUI components; tests wire it to a list collector.

## Modules

| Module | Purpose | Key files |
|---|---|---|
| `enhancer.core` | The 4-pass pipeline + parsers + budgeting + frozen `EventType` enum | `pipeline.py`, `passes.py`, `transforms.py`, `parsing.py`, `budgeting.py`, `events.py` |
| `enhancer.llm` | Provider abstraction + LM Studio impl + stubs | `base.py`, `lmstudio.py`, `ollama.py`, `openai.py`, `anthropic.py`, `registry.py` |
| `enhancer.persistence` | SQLite primary, JSONL dual-write, atomic JSON helper | `db.py`, `schema.sql`, `runs.py`, `sessions.py`, `jsonl_compat.py`, `safestorage.py` |
| `enhancer.cli` | typer entrypoint + extras (batch / compare / export) | `main.py`, `extras.py` |
| `enhancer.ui` | NiceGUI Desktop Studio | `app.py`, `pages/*.py`, `components/*.py` |
| `enhancer.observability` | structlog placeholder + optional OTEL hooks | (v0.2) |
| `enhancer.config` | Env-driven settings dataclass | `config.py` |

## Pipeline ordering

```
[user prompt]
     │
     ▼  (truncate + session-context wrap)
┌────────── Pass 1 — Intent Analysis ──────────┐
│ provider.chat_stream → parse_task_type      │
└──────────────────────────────────────────────┘
     │ SERIAL — never parallel (LM Link queues)
     ▼
┌────────── Pass 2 — Weakness Detection ───────┐
│ provider.chat_stream → parse_technique +    │
│ count_weakness_fields                       │
└──────────────────────────────────────────────┘
     │
     ▼  if count >= 3
┌────────── Disambiguation generation ─────────┐
│ provider.chat → parse_disambiguate_questions│
│ on_event(AGENT_DISAMBIGUATE) → PAUSE         │
└──────────────────────────────────────────────┘
     │ caller resumes via opts.resume_state
     ▼
┌────────── Persona (optional) ────────────────┐
│ provider.chat_stream → parse_persona        │
└──────────────────────────────────────────────┘
     │
     ▼
┌────────── Pass 3 — Task-aware Rewrite ───────┐
│ provider.chat_stream                        │
│ (Pass 4 fired as background task here)      │
└──────────────────────────────────────────────┘
     │
     ▼
┌────────── AWAIT Pass 4 — Quality Scoring ────┐
│ MUST complete BEFORE Magnitude/SoT          │
└──────────────────────────────────────────────┘
     │
     ▼
   (optional) Self-correction retry
   (optional) Magnitude blueprint stream
   (optional) Skeleton-of-Thought stream
     │
     ▼
[PipelineResult]  +  on_event(AGENT_DONE)
```

The three load-bearing concurrency invariants are enforced verbatim and
guarded by `tests/test_concurrency.py`. See
[`docs/EXTRACTION_GOTCHAS.md`](EXTRACTION_GOTCHAS.md) for the full
rationale and `.claude/knowledge/lm-link-concurrency.md` for the field
report.

## Data flow

1. **Caller** (CLI / UI) constructs a `ChatProvider` via
   `llm.registry.get_provider(settings)` and a `PipelineOptions`.
2. **`run_pipeline()`** orchestrates passes, emits events through
   `on_event`, and returns a `PipelineResult` envelope.
3. **Caller** persists the result via
   `persistence.runs.save(record, db_path, jsonl_log_path)` if desired.
   The dual-writer keeps the monolith's `agent_pipeline.log` consumers
   working during the v0.1 → v1.0 migration window.
4. **Studio / CLI** reads back via `persistence.runs.list_recent()` /
   `runs.get_run()` / `runs.stats()`.

## Storage layout

| Path | Purpose |
|---|---|
| `%APPDATA%\prompt-enhancer\enhancer.db` | SQLite — runs, scores, sessions, templates |
| `%APPDATA%\prompt-enhancer\agent_pipeline.log` | JSONL dual-write (one-release deprecation) |
| `%APPDATA%\prompt-enhancer\settings.toml` | (v0.2) persisted settings |

(`%APPDATA%` resolves via `platformdirs.user_data_dir("prompt-enhancer",
appauthor=False)` so Linux/macOS get the right XDG paths automatically.)

## Settings flow

1. `enhancer.config.load() → Settings` reads `ENHANCER_*` env vars on
   each call (no module-level singleton — that bug bit the monolith).
2. The CLI / UI passes `Settings` to the provider registry which
   instantiates the right backend.
3. The pipeline reads only the values it needs (timeout, idle_timeout,
   default_model, scorer_model) — settings are never module-global.

## Extension points

- **New provider:** subclass `ChatProvider`, register in
  `enhancer.llm.registry.get_provider()` (or via the
  `enhancer.providers` entry-point group in v0.2).
- **New transform pass:** add a function in `core/transforms.py`,
  optionally guarded by an `opts.<flag>_mode` switch in
  `PipelineOptions`. Emit a fresh `EventType` member if it streams.
- **New CLI command:** add to `cli/extras.py` and call
  `register(app)` from `cli/main.py`.
- **New UI page:** add a module under `ui/pages/`, wire a route in
  `ui/app.py`.

## Why these choices

- **Async-only core** — every LLM call is `await`-driven; the streaming
  paths have backpressure. No threadpools.
- **Frozen event contract** — adding events is fine; renaming requires
  a v2 namespace bump. This is the API the monolith's `devflow.py` and
  `chain_events.py` consumers read.
- **SQLite over a server DB** — single-user product; WAL + 5-second
  busy_timeout cover concurrent CLI + UI use without an external
  service.
- **NiceGUI for the UI** — Python all the way down; the same async
  loop that runs the pipeline renders the Studio. Tauri/Electron would
  have added a JavaScript build step we don't need.
- **`platformdirs` for paths** — no hardcoded `%APPDATA%` or
  `~/.config`. Linux + macOS support comes for free.
