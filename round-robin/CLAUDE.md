# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**Round Robin** — a single-window desktop app for orchestrating dialogue between two LLMs running on separate machines connected via **LM Studio's LM Link** mesh, with an optional third agent (Charlie) that summarizes the finished transcript into a structured `summary.md` for handoff to **FTSIA** (Folder Tree Structure Integrity Administration) — the downstream framework that turns the summary into modular skeletons.

Lifted out of the larger `swarm-agent-dev` mod (`src/webui/mods/round_robin.py`) and rebuilt with cleaner UX: visible turn progress, pause/inject between turns, graceful per-agent error recovery, dialogue intelligence (anti-rambling + anti-yes-man), full error monitoring, color-coded host distinction.

**Stack:** Python 3.11+ · FastAPI · uvicorn · httpx (async) · pywebview · pydantic · pytest + pytest-asyncio.

**Process model:** `app.py` starts uvicorn in a background thread on a random localhost port, then opens a pywebview window pointed at it. One process, one window, no browser tab, no port to remember.

## Commands

### First-time setup
Double-click `Start.bat` — it creates the venv, installs `pip install -e ".[dev]"`, and launches the desktop window.

### Manual
```
.venv\Scripts\activate
python app.py              # launches the desktop window
pytest -v                  # full test suite (162 tests as of 2026-05-04, v0.1.0)
pytest tests/test_intel.py # one file
```

## Architecture

| Module | Purpose |
|---|---|
| [src/round_robin/config.py](src/round_robin/config.py) | Paths (`DATA_DIR`, `STATE_FILE`, `PRESETS_FILE`, …), `LMS_BASE_URL` (env-overridable), timeout knobs |
| [src/round_robin/storage.py](src/round_robin/storage.py) | `SafeStorage.save_json` / `load_json` — atomic temp+rename writes with `.bak` recovery on corruption |
| [src/round_robin/lm_client.py](src/round_robin/lm_client.py) | `LMLinkClient` — single `httpx.AsyncClient` to `localhost:1234/v1`. Methods: `chat()`, `chat_stream()` (SSE), `models()`, `health()` |
| [src/round_robin/health.py](src/round_robin/health.py) | `probe()` — combines `/v1/models` + `lms link status`. Tags each model with `is_local: bool` (first device from CLI status = local). Used by `/api/health` |
| [src/round_robin/lms_cli.py](src/round_robin/lms_cli.py) | `lms_link_status()` — best-effort subprocess wrapper around the `lms` CLI. Never raises; returns `None` if missing |
| [src/round_robin/intel.py](src/round_robin/intel.py) | `DialogueAnalyzer`, `Nudge`, `IntelConfig`, `COLLAB_DIRECTIVE`. Pure functions: closure/agreement detection, redundancy scoring, nudge selection |
| [src/round_robin/orchestrator.py](src/round_robin/orchestrator.py) | `Orchestrator` state machine. Statuses: `idle → running → paused → awaiting_user → done/stopped/error`. Methods: `start`, `stop`, `pause`, `resume`, `submit_choice`, `regenerate_summary`. Owns the dialogue loop, end-of-run Charlie summary, intel nudge injection, agreement-streak tracking |
| [src/round_robin/sessions.py](src/round_robin/sessions.py) | `PresetStore` (CRUD + rename + duplicate + import/export) and `SessionStore` (auto-save every run as `data/sessions/run-<ts>.json`, list/load/search/delete) |
| [src/round_robin/user_config.py](src/round_robin/user_config.py) | UI preferences store. `load()`/`save()`/`reset()` against `data/config.json`. Allowlist of accepted keys; partial PATCH semantics. Frontend auto-saves to this on every config-control change |
| [src/round_robin/monitoring.py](src/round_robin/monitoring.py) | `ErrorMonitor` — JSONL append to `data/errors.log` (rotates at 5MB), in-memory ring of last 200 events, async broadcast hook. Captures from FastAPI `@app.exception_handler`, asyncio loop exception handler, and any `*_error` WebSocket event auto-intercepted in `server.py` |
| [src/round_robin/charlie/workspace.py](src/round_robin/charlie/workspace.py) | Sandbox: per-session folder under `data/charlie_workspace/session-<ts>/`. Rejects `..`, absolute paths, drive letters, hidden names, > 2 MB files, symlink escapes |
| [src/round_robin/charlie/agent.py](src/round_robin/charlie/agent.py) | `CharlieAgent.summarize()` — non-streaming LLM call → structured Markdown with YAML frontmatter (`run_id`, `theme`, `model`, `schema_version`) and six fixed H2 sections (Theme, Participants, Resolved Decisions, Proposed Module Breakdown, Open Questions, Full Transcript). Writes `summary.md` for FTSIA. Auto-fires once when the orchestrator reaches `run_done` (auto + manual re-run via `POST /api/charlie/summarize`) |
| [src/round_robin/server.py](src/round_robin/server.py) | FastAPI app: `/`, `/api/health`, `/api/models`, `/api/run/*`, `/api/presets/*`, `/api/sessions/*`, `/api/errors`, `/api/charlie/file`, `POST /api/charlie/summarize` (manual re-run), `/ws`. Wraps the orchestrator's emit() to auto-capture `*_error` events into the monitor |
| [src/round_robin/static/](src/round_robin/static/) | `index.html` + `app.css` + `app.js` (~80-line event-handler registry — flat dispatch over WS events). No build step |
| [app.py](app.py) | Desktop launcher: free port → uvicorn thread → wait-until-ready → pywebview window |

## LM Link transport (the key detail)

All HTTP requests go to **`http://localhost:1234/v1`** — every agent. LM Studio internally routes the request to the remote machine when the requested `model` identifier lives there. Per-machine targeting is implicit through model names + `lms link set-preferred-device`.

**Implication:** there is no `host_url` field per agent. Just pick the right model — the dropdown groups models by host (Alpha = local, Bravo = remote device names from `lms link status`).

If LM Link Preview isn't enabled, the app still works against just Alpha (single-machine round robin) and shows a yellow banner.

## Persistence files

| Path | Contents |
|---|---|
| `data/state.json` | Current run state (live during a run; lets the UI reattach after window close/reopen) |
| `data/config.json` | UI prefs: theme, last-used models, intel toggle defaults, etc. Frontend auto-saves (debounced 500 ms) and restores at boot |
| `data/presets.json` | `[{id, name, config, created_at, updated_at}, …]` (max 200) |
| `data/sessions/run-<ts>.json` | Auto-saved on `run_done`. Full transcript + config snapshot |
| `data/errors.log` | JSONL append; rotates at 5 MB → `.1` → `.2` → `.3` |
| `data/charlie_workspace/session-<ts>/summary.md` | Charlie's structured summary for FTSIA — YAML frontmatter + 6 fixed H2 sections |

All JSON written via `SafeStorage` (`.tmp` → atomic rename, `.bak` for recovery from corruption).

## WebSocket event schema

```
hello              { state }                                # on connect
run_started        { run_id, config }
turn_started       { turn, agent_name, model, total_turns }
turn_chunk         { turn, agent_name, token }
turn_done          { turn, agent_name, content, latency_ms, token_count }
run_paused         { reason }
run_resumed        { injection? }
agent_error        { turn, agent_name, error_class, message, auto_retry? }
charlie_started    { session_id, run_id }                    # summary generation begins
charlie_done       { run_id, path, tree, session_id }        # summary.md written
charlie_error      { error }
dialogue_nudge     { reason, content, turn, after_agent }   # intel injection
error_logged       { event: { id, timestamp, category, severity, message, context } }
run_done           { run_id, status, turns_completed, error? }
pong               {}
```

Frontend dispatch lives in [src/round_robin/static/app.js](src/round_robin/static/app.js) via `on(event, handler)` registry.

## Dialogue intelligence

[intel.py](src/round_robin/intel.py) provides three pure-function detectors that the orchestrator runs after every `turn_done`:

- **Closure detection** — regex matches against ~15 closure phrases (`"in summary"`, `"let me know if"`, etc.). Fires a `closure` nudge.
- **Redundancy detection** — Jaccard word-overlap (stopwords removed) between this turn and the agent's previous turn. Default threshold 0.7. Fires a `redundant` nudge.
- **Brevity detection** — turns under 30 tokens past the warm-up. Fires a `brief` nudge.
- **Agreement-streak** — orchestrator tracks consecutive turns containing agreement signals; threshold 2 → fires a `contrarian` nudge that asks the next agent to take the opposite stance.

Plus the `COLLAB_DIRECTIVE` system-prompt addendum (toggleable via `intel_collab_directive`) that asks every agent to be a critical collaborator instead of yes-man.

Last-turn guard: nudges never fire when there's no agent-turn left to respond to them.

## Gotchas

- **`.bat` line endings:** Windows `cmd.exe` chokes on multi-line `if (...)` paren-blocks when the file uses LF endings. Use `goto :label` jumps instead, or ensure CRLF. `Start.bat` is now CRLF + goto-style.
- **pywebview + WebView2:** WebView2 runtime is preinstalled on Windows 11. If the desktop window fails to open on Win 10, install WebView2 from Microsoft.
- **LM Studio JIT-load latency:** first call against a not-yet-loaded model can take many seconds. Default read timeout is 300 s.
- **`lms link status` is best-effort:** parsing varies by LM Studio version. Returns `None` if the CLI is missing or the parse fails — the app still works (Bravo just shows as offline).
- **Auto-scroll:** `appendToken` only scrolls if the user is at the bottom AND has no active text selection. This makes streaming text selectable and copy-pasteable. Don't revert to per-token `scrollIntoView` on the card element — it kills selections.
- **Per-agent host:** the model dropdown shows colored host badges (green = Alpha/local, blue = Bravo/remote). Same colors flow through to the turn-card left border so you can scan a transcript and see which machine produced each turn.
- **Pre-existing test counts (regression check):** 97 tests should pass. Sub-categories: `test_storage` (4), `test_lm_client` (6), `test_charlie_workspace` (10), `test_charlie_summary` (13 — summarizer + orchestrator end-of-run wiring), `test_orchestrator` (11), `test_monitoring` (7), `test_intel` (15), `test_user_config` (7), `test_server_routes` (10), `test_health` (8), `test_lms_cli` (6).
- **Crash recovery is "view, not resume":** state.json is updated every turn, so a crash mid-run preserves the transcript. On boot `/api/state` checks if the saved status is `running`/`paused`/`awaiting_user`; if yes, the UI shows a banner with View / Discard. There is no auto-resume because LM Studio's per-model context is gone after the process restarts.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LMS_BASE_URL` | `http://localhost:1234/v1` | LM Studio OpenAI-compatible endpoint |
| `LMS_TIMEOUT_CONNECT` | `5.0` | Connect timeout (s) |
| `LMS_TIMEOUT_READ` | `300.0` | Read timeout (s) — generous for JIT loads |
| `LMS_TIMEOUT_WRITE` | `30.0` | Write timeout (s) |
| `ROUND_ROBIN_DATA_DIR` | `<repo>/data` | Override the data directory |
