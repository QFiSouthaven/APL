# Track Record

Lightweight progress tracker for the Round Robin project. The snapshot at the
top is the answer to "what's the current state?". The table below is the
running log — append a row each time a feature lands.

---

## Current State Snapshot — 2026-04-26

| | |
|---|---|
| **Project root** | `C:\Users\Falki\round-robin\` |
| **Stack** | Python 3.11+ · FastAPI · uvicorn · httpx · pywebview · pydantic |
| **Process model** | Single process. uvicorn in a background thread on a random port; pywebview window points at it |
| **Source files** | 18 Python/HTML/CSS/JS modules (~2,900 LoC) |
| **Test files** | 8 modules · **70 tests · all passing** |
| **Persistence dir** | `data/` — `state.json`, `config.json`, `presets.json`, `sessions/`, `errors.log` (rotates 5 MB), `charlie_workspace/` |
| **REST routes** | 24 |
| **WS events** | 13 typed (`run_started`, `turn_started/chunk/done`, `run_paused/resumed`, `agent_error`, `charlie_started/op/done/error`, `dialogue_nudge`, `error_logged`, `run_done`) + `hello` + `pong` |
| **Launch** | Double-click `Start.bat` (CRLF, goto-style; LF-safe) |
| **External deps** | LM Studio on port 1234 with at least one model loaded. LM Link Preview optional (Bravo appears as offline if missing) |

### Smoke health (last verified)

- `python app.py` opens the desktop window
- `pytest -q` → 53 passed in ~2.7 s
- `GET /api/health` against real LM Studio returns model list
- `GET /api/state` returns current orchestrator state
- `WS /ws` returns `hello` then `pong` to `ping`
- `POST /api/presets` + `DELETE /api/presets/<id>` round-trip

---

## Progress Table

| Date | Phase | Feature | Status | Notes |
|---|---|---|---|---|
| 2026-04-26 | 1 | Plan + research (LM Link, codebase exploration) | ✅ done | 3 parallel exploration agents, AskUserQuestion x4 |
| 2026-04-26 | 2 | Project scaffold | ✅ done | pyproject, README, package layout |
| 2026-04-26 | 2 | `SafeStorage` (atomic JSON + .bak) | ✅ done | Ported from `swarm-agent-dev/src/core/system.py` |
| 2026-04-26 | 2 | `LMLinkClient` (httpx + SSE) | ✅ done | Single client per instance, http2=False, generous read timeout |
| 2026-04-26 | 2 | `health.py` + `lms_cli.py` | ✅ done | `/v1/models` + `lms link status` parsed best-effort |
| 2026-04-26 | 2 | `Orchestrator` state machine | ✅ done | idle→running→paused→awaiting_user→done; pause/resume/retry/skip/use_other |
| 2026-04-26 | 2 | Charlie sandbox + agent | ✅ done | Per-session folder, path traversal rejected, 2 MB cap, JSON ops parser |
| 2026-04-26 | 2 | `PresetStore` + `SessionStore` | ✅ done | CRUD + rename + duplicate + import/export + auto-save runs + search |
| 2026-04-26 | 2 | FastAPI server (REST + WS) | ✅ done | 19 routes initially; `/ws` event broadcaster |
| 2026-04-26 | 2 | Frontend (HTML/CSS/JS) | ✅ done | Sidebar tabs, top health pills, sticky turn progress, error-recovery buttons inline |
| 2026-04-26 | 2 | `app.py` desktop launcher | ✅ done | uvicorn in thread + pywebview window |
| 2026-04-26 | 3 | Test suite (storage, lm_client, orchestrator, charlie) | ✅ done | **26 tests** initial |
| 2026-04-26 | 3 | Live smoke against real LM Studio | ✅ done | Confirmed against `gemma-4-26b-…` model |
| 2026-04-26 | 4 | `Start.bat` v1 | ❌ broken | Multi-line `if (...)` + LF endings → `... was unexpected at this time.` |
| 2026-04-26 | 4 | `Start.bat` v2 — `goto :label` + CRLF | ✅ done | LF-safe; `where python` precheck; `pip show` install detect; final `pause` always |
| 2026-04-26 | 5 | Error monitoring | ✅ done | `monitoring.py` + `/api/errors` + Errors tab + auto-capture from `*_error` events + global handlers |
| 2026-04-26 | 5 | Error monitoring tests | ✅ done | **+7 tests = 33 total** |
| 2026-04-26 | 6 | Stop-hook fix in `swarm-agent-dev` | ✅ done | Switched 3 hook commands to absolute paths |
| 2026-04-26 | 7 | Color-coded model dropdowns (Alpha green / Bravo blue) | ✅ done | `<optgroup>` grouping + sibling `host-badge` chip; same colors flow through to turn-card left border + health pill dots |
| 2026-04-26 | 7 | Selectable streaming text + Copy button | ✅ done | Replaced per-token `scrollIntoView` with smart bottom-pin; `user-select: text` on cards; navigator.clipboard Copy button per card |
| 2026-04-26 | 7 | Anti-rambling nudges (closure / redundant / brief) | ✅ done | `intel.py` regex detectors → `dialogue_nudge` WS event → italic separator card in transcript. Last-turn guard. Toggleable |
| 2026-04-26 | 7 | Anti-yes-man (contrarian after agreement streak) | ✅ done | Streak tracked across turns in orchestrator; threshold default 2; resets after firing |
| 2026-04-26 | 7 | Critical-collaborator directive baked into personas | ✅ done | `COLLAB_DIRECTIVE` prepended in `_build_messages` when toggle on |
| 2026-04-26 | 7 | Intel tests + orchestrator tests | ✅ done | **+15 + 5 = 53 total**, all green |
| 2026-04-26 | 7 | Docs: `CLAUDE.md`, `SESSION_LOG.md`, `TRACK_RECORD.md` | ✅ done | Evergreen reference + session chronology + this file |
| 2026-04-26 | 8 | Crash-recovery banner | ✅ done | Backend `/api/state` now filters resumable to in-flight statuses only; frontend surfaces banner with View / Discard. New `DELETE /api/state` route |
| 2026-04-26 | 8 | UI prefs persistence (`data/config.json`) | ✅ done | New `user_config.py` module + `GET/PATCH /api/config`. Frontend debounced auto-save (500 ms) on every config control. Restored at boot |
| 2026-04-26 | 8 | Intel nudges distinguishable in replay | ✅ done | `replaySession` checks `intel_reason` and renders the purple `nudge-card`; manual user nudges keep the plain card |
| 2026-04-26 | 8 | Session export button | ✅ done | "Export" button on each History row downloads `<theme>_<run_id>.json` |
| 2026-04-26 | 8 | Model-clash warning suppressed when only 1 model | ✅ done | No real choice → no real warning |
| 2026-04-26 | 8 | Scroll-tracking reset on new run | ✅ done | `clearDialogue` resets `state.userScrolledUp` so a stale scroll position from a previous run doesn't suppress auto-scroll on the new one |
| 2026-04-26 | 8 | Tests: user_config + server_routes | ✅ done | **+17 tests = 70 total**, all green |

---

## How to add a row

When a new feature lands:

```markdown
| 2026-MM-DD | <phase #> | <feature name> | ✅ done | <one-line note: tests/files touched/key detail> |
```

Use ❌ for broken/abandoned, 🚧 for in-progress, ⏸ for paused.

Update the **Current State Snapshot** at the top whenever counts change (new test files, new dependencies, new persistence categories).
