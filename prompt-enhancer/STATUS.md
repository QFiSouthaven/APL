# Build Status

_Auto-updated by the implementation thread; reviewed by the Methodology
Enhancement Agent on every diff._

## Phase progress

| Phase | Status | Notes |
|---|---|---|
| 0 — scaffold | ✅ done | `pyproject.toml`, `src/` layout, configs, methodology agent hook |
| 1 — extract core | ✅ done | helpers, system prompts, pipeline + disambig pause/resume, all three concurrency invariants |
| 2 — SQLite persistence | ✅ done | schema, db, runs, sessions, jsonl_compat, safestorage |
| 3 — ChatProvider abstraction | ✅ done | LM Studio fully implemented; Ollama/OpenAI/Anthropic stubs with install hints |
| 4 — typer CLI | ✅ done | `version` `models` `enhance` `history` `ui` `batch` `compare` `export` + interactive disambiguation Q&A + `--skip-clarify` flag |
| 5 — NiceGUI Desktop Studio | ✅ done | Studio (status strip + tabs + sliders + diff view + 6 components), History (with branch_tree row-detail), Analytics, Compare, Templates (8 seeds), Settings, disambiguation modal |
| 6 — packaging | ✅ done | `dist/prompt-enhancer/prompt-enhancer.exe` rebuilt 2026-05-03 against Python 3.12 (117 MB, smoke=HTTP 200). Inno installer compiled to `release/prompt-enhancer-setup.exe` (38 MB, SHA256 `96a6ff106bc235f5ec3d678d1f00f1db834e510cfd54a0b86460db44e7d86198`) using Inno Setup 6.7.1. Includes the `tomli-w` runtime-dep fix from commit `20112ff`. |
| 7 — verification | ✅ **LIVE-TESTED** | **338 prompt-enhancer + 162 round-robin + 224 development tests collected** (umbrella total: 724 fast tests, +1 LM-Studio integration test deselected by default; re-collected 2026-05-04 after v2.2 ReasoningPanel work — Pass 1/2/3/4 wiring in prompt-enhancer, all 5 stages in development, Charlie + panel-per-voice in round-robin) |
| 8 — LM Studio discovery + auto-load | ✅ done | `src/enhancer/llm/lms_discovery.py` + 10 tests. Calls `/api/v0/models` for state-aware listing, falls back to `lms load` CLI shell-out when nothing is loaded, raises `ModelLoadUnavailableError` with operator instructions on failure. Wired into CLI `enhance`, NiceGUI startup, and the methodology-agent Stop hook. |
| 9 — provider-layer resilience | ✅ done | `src/enhancer/llm/resilience.py` + 16 tests. `@with_retry` + `@with_stream_retry` decorators (exp-backoff, ±25 % jitter, 3 retries, honors `Retry-After` on 429); `ProviderHealth` circuit-breaker opens after 3 consecutive final failures, 30 s cooldown. Session counters surfaced to the Studio session drawer. Pipeline invariants in `core/pipeline.py` are NOT touched — wrap is at the provider layer. |
| 10 — multi-backend providers (v1.1) | ✅ done | `src/enhancer/llm/{ollama,openai,anthropic}.py` real implementations replacing v1.0 NotImplementedError stubs. All three retry-wrapped. OpenAI uses `httpx` direct (skips SDK weight); Anthropic targets native `/v1/messages` shape with system-role lifting and typed-SSE parsing, also reaches LM Studio's compat endpoint via `ENHANCER_ANTHROPIC_BASE_URL`. 23 conformance tests in `tests/test_providers.py`. |
| 11 — observability layer (v1.1) | ✅ done | `src/enhancer/observability/__init__.py` exposes `configure_logging()` (idempotent structlog setup, JSON for non-TTY, colored otherwise), `get_logger()` re-export, `trace_block(name, **attrs)` context manager, `traced(name=None)` decorator (auto-detects async). OTEL is strictly soft — gated on `OTEL_EXPORTER_OTLP_ENDPOINT`; opentelemetry-* libs never import unless that env var is set. |
| 12 — APL umbrella coordination (v1.2) | ✅ done | `APL/.gitignore`, `APL/README.md`, `APL/lab/onboarding.py` (seeds shared `services.toml`), and `APL/lab/launch.py` (orchestrated boot — spawns each component as subprocess, polls `/api/health`, clean shutdown on Ctrl-C). round-robin tracked under the umbrella; got its own `discovery.py` mirroring prompt-enhancer's, plus `/api/peers` + `/api/health` endpoints (additive — preserved its existing /api/health body). round-robin port fix: `_free_port()` → discovery-aware port (8766 default). |
| 13 — task-aware scorer + multi-host (v1.2) | ✅ done | `src/enhancer/llm/model_router.py` with `select_scorer(task_type, models, preferred)` and substring-based routing rules per task_type. `src/enhancer/llm/lms_discovery.py` extended with `discover_chat_models_multihost(hosts)` and `pick_loaded_host(hosts, preferred)` for LAN-spanning discovery. 36 new tests (28 router + 8 multi-host). Wiring into pipeline.py Pass 4 deferred to v1.2.x follow-up. |
| 14 — REST expansion + plugin entry-points (v1.2) | ✅ done | `src/enhancer/api/rest.py` gains `/api/runs`, `/api/runs/{id}`, `/api/sessions`, and `/api/forward-to/{peer}` (cross-component invocation). `src/enhancer/llm/registry.py` consults `enhancer.providers` entry-point group so third-party packages register via `[project.entry-points."enhancer.providers"]` without modifying enhancer code. Compat shim handles `importlib.metadata.entry_points` shape difference between Python 3.10 and 3.12. 19 new tests. |
| 15 — EventType v2 + MIGRATION (v2.0) | ✅ done | Frozen 30-member EventType enum gains 6 additive members across 3 new groups: `PROVIDER_HEALTH_OPEN`/`CLOSED`, `MCP_TOOL_INVOKED`/`RESULT`, `BRANCHING_FORK`/`MERGE`. v1 ordering preserved byte-for-byte. `docs/MIGRATION.md` documents the v1→v2 transition + 36-member reference table + compatibility commitment (v2.x emits all v1 names; v3.0 may remove deprecated members with one minor's warning). 5 new tests in `test_events.py`. |
| 16 — MCP client subpackage (v2.0) | ✅ foundation done | New `src/enhancer/mcp/` subpackage: `MCPClient` (JSON-RPC 2.0 over HTTP via httpx, retry-wrapped via existing resilience layer), `MCPRegistry` (multi-server orchestrator with partial-failure tolerance), `MCPToolInvoker` (Pass 1/3 hook surface — emits `MCP_TOOL_INVOKED`/`RESULT` with EventType-fallback to literal strings). HTTP transport ONLY in v2.0; stdio deferred to v2.1. 23 tests. **Pipeline integration deferred to v2.0.1.** |
| 17 — TOML pipeline graph + validator (v2.0) | ✅ foundation done | New `src/enhancer/core/pipeline_graph.py`: `PassNode` + `PipelineGraph` frozen dataclasses, `load(path)`, `default_graph()`, and a static `validate()` that rejects configs at LOAD time if they would violate the three frozen concurrency invariants. 6 distinct failure modes each with a documented error keyword. `docs/PIPELINE_GRAPH.md` is the user-facing schema reference. 27 tests. **Pipeline integration deferred to v2.0.1.** |
| 18 — `enhancer.transforms` entry-point group (v2.0) | ✅ done | Second entry-point group alongside `enhancer.providers`. `discover_transforms()` returns plugin classes registered under `enhancer.transforms`. Duck-checked (callable OR has `.apply()`); failures logged + skipped. Built-in transforms (Persona, Magnitude, SoT) remain inline; this surface is for THIRD-PARTY plugins. 6 new tests. |
| 19 — UI test harness + smoke coverage (v2.0) | ✅ done | Closed the historic UI testing gap. 53 tests across `test_ui_pages.py` (6 page modules) + `test_ui_components.py` (6 component modules). `ui_tmp_db` fixture in `conftest.py` redirects DB path to `tmp_path` so render() smoke tests don't touch real user data. Decision record at `docs/UI_TESTING.md`: import-only smoke for v2.0; `nicegui.testing` interaction harness deferred to v2.1. |
| 20 — pipeline wirings (v2.0.1) | ✅ done | Three optional `run_pipeline` parameters that preserve all pre-2.0.1 behavior when `None`: `pipeline_graph` (validated at call-time via `core.pipeline_graph.validate`; rejects invariant-violating configs BEFORE any LLM call); `mcp_invoker` + `mcp_pre_pass1` / `mcp_pre_pass3` (calls MCP tools and stitches results into the user message via `[MCP CONTEXT]…[END MCP CONTEXT]` block — failures swallowed so a misbehaving MCP server can't break a pipeline run); `model_router` auto-selection for Pass 4 scorer (when `opts.scorer_model` is empty, picks task-aware scorer from `provider.list_models()`; falls back to `model` on failure). 9 new tests in `test_pipeline_v201.py`. **All three frozen concurrency invariants survived intact** — `tests/test_concurrency.py` passes unchanged. |
| 21 — ReasoningPanel abstraction (v2.1) | ✅ done | New `src/enhancer/llm/reasoning_panel.py`: `LLMSlot`, `ReasoningPanel`, `PanelResult`, `SlotResponse`. Three modes (`primary-only` / `parallel` / `sequential`), three aggregators (`primary-wins` / `longest` / `consensus-vote`). Heterogeneous panels (different providers/models/hosts per slot) are first-class; partner failures are captured per-slot and never propagate. 24 tests in `test_reasoning_panel.py`. Re-exported by round-robin (`round_robin/reasoning_panel.py`) and consumed by development (`development.stages.base._chat_or_panel`). |
| 22 — ReasoningPanel pipeline wirings (v2.2) | ✅ done | `run_pipeline` gains optional `reasoning_panel`, `panel_mode`, `panel_aggregator` kwargs. Pass 1 / Pass 2 / Pass 4 route through `panel.consult` when supplied (commit `50dfb5e`). Pass 3 streams the primary's tokens live + runs partners non-streaming in parallel for telemetry — `primary-wins` by design (commit `7f58aad`). All telemetry lands in `result.extras["panel"][<pass_key>]` with the canonical `{primary, partners: [{name, content, ms, error}]}` shape. Sibling components ship matching wirings: development v2.2 (Architect/Coder/Reviewer/Tester/Packager — all 5 stages); round-robin (Charlie voice + panel-per-voice in `/api/review`). User-facing reference: **`docs/REASONING_PANEL.md`**. Partner timeouts on Pass 3 are bounded by `request_timeout`. Round-robin handoff helper + multi-host CLI/UI wiring shipped alongside (commits `fb49b86`, `66064c7`, `f0389be`). All three concurrency invariants still pass — `tests/test_concurrency.py` unchanged. |

## Test status (re-collected 2026-05-04, Python 3.12 dev venv, v2.2)

prompt-enhancer suite (`prompt-enhancer/tests/`) — **338 tests** across
28 test files (1 deselected by default: `test_integration_panel_lmstudio.py`,
which talks to a real LM Studio):

```
test_api_rest, test_branching, test_cli_auto_resume, test_concurrency,
test_config_toml, test_disambiguation, test_discovery, test_events,
test_integration_panel_lmstudio (deselected), test_lms_discovery,
test_lms_host_picker_wiring, test_mcp, test_migration, test_model_router,
test_parsing, test_pipeline_graph, test_pipeline_panel, test_pipeline_smoke,
test_pipeline_v201, test_providers, test_reasoning_panel, test_registry,
test_resilience, test_round_robin_handoff, test_ui_components, test_ui_pages
                                              ────────────
                                              338 collected
```

development sibling (`development/tests/`): **224 tests** (panel wiring
across all 5 stages — v2.2.0 release commit `b85f05c`).

round-robin sibling (`round-robin/tests/`): **162 tests** (Charlie voice
+ panel-per-voice in `/api/review` — commit `2b12718`).

**Umbrella total: 724 tests across three components.** When in doubt,
collect fresh: each component has its own `.venv/` and `pytest -q --collect-only`.

**Build-env note:** dev venv was rebuilt fresh on 2026-05-02 against Python 3.12.0 (commit `3a6fa8e`). The previous venv ran on Python 3.13 — the bundled exe in `packaging/dist/` still carries 3.13 `.pyd` files and may need a rebuild before shipping.

## Live verification — 2026-04-28 against gpt-oss-120b via LM Link

### Run 1 — initial round-trip (run id `5289124687aaae92`)

```
$ enhancer enhance "Make me a customer-support chatbot for a small SaaS startup" \
        --skip-clarify --tokens 1.5

Pass 1 (Intent Analysis) ─ 12.7 s (544 ch streamed)
Pass 2 (Weakness Detection) ─ 12.7 s (916 ch streamed)
Disambiguation generation ─ 18.2 s (3 weakness fields → pause)
[--skip-clarify resumed with empty answers]
Pass 3 (Prompt Rewrite) ─ 49.0 s (2842 ch enhanced prompt)
Pass 4 (Quality Scoring) ─ 14.7 s (NON-streaming chat → empty content;
                                    scores_fallback=true)
```

### Run 2 — Pass 4 streaming (run id `b555e5225b385f0d`)

After switching Pass 4 from non-streaming `chat()` to `chat_stream()`
to bypass LM Studio's reasoning-token filter:

```
Pass 1 ─ 14.1 s
Pass 2 ─ 13.1 s
Pass 3 ─ 56.1 s (3486 ch enhanced prompt)
Pass 4 ─ 15.5 s (STREAMING → scores returned reliably)

scores_fallback:  0     ← false! gpt-oss returned the scores
specificity:      9
constraints:      10
actionability:    10
improvement:      92%
```

Per-pass durations tracked **individually** (the timing fix); pass1
and pass2 are independent measurements, not the averaged-half values
from before.

`gen_score` budget bumped 200 → 400 tokens to give reasoning-token
models headroom past their internal thinking.

## File tree (final)

```
prompt-enhancer/
├── pyproject.toml, README.md, STATUS.md, .gitignore
├── docs/
│   └── EXTRACTION_GOTCHAS.md            (methodology-agent guard rail)
├── src/enhancer/
│   ├── __init__.py, config.py
│   ├── core/
│   │   ├── events.py                    (FROZEN 30-member EventType enum)
│   │   ├── parsing.py                   (clamp, parsers, disambig Q&A)
│   │   ├── budgeting.py                 (truncate, context detection, pass budgets)
│   │   ├── passes.py                    (PASS1-4 + technique guidance)
│   │   ├── transforms.py                (PERSONA, MAGNITUDE, SOT, PRETRIAL)
│   │   └── pipeline.py                  (run_pipeline + run_pretrial — main loop)
│   ├── llm/
│   │   ├── base.py                      (ChatProvider ABC)
│   │   ├── lmstudio.py                  (LM Studio + LM Link, idle_timeout=120)
│   │   ├── lms_link.py                  (LM Link discovery / handshake helper)
│   │   ├── ollama.py, openai.py, anthropic.py  (stubs)
│   │   └── registry.py
│   ├── persistence/
│   │   ├── schema.sql, db.py, runs.py, sessions.py
│   │   ├── jsonl_compat.py              (devflow.py byte-for-byte compat)
│   │   └── safestorage.py
│   ├── observability/__init__.py
│   ├── api/                             (NEW — shipped post-STATUS-2026-04-28)
│   │   ├── rest.py                      (REST endpoints over the pipeline)
│   │   └── discovery.py                 (provider/model discovery service)
│   ├── cli/
│   │   ├── main.py                      (typer entry)
│   │   └── extras.py                    (batch / compare / export)
│   └── ui/
│       ├── app.py                       (NiceGUI router + dark CSS)
│       ├── pages/
│       │   ├── studio.py                (status strip + tabs + sliders + live stream + diff)
│       │   ├── history.py               (filterable run table)
│       │   ├── analytics.py             (KPIs + technique pie + task-type bar)
│       │   ├── compare.py               (side-by-side scorecard — was v0.2, now shipped)
│       │   ├── templates.py             (CRUD over templates table — was v0.2, now shipped)
│       │   └── settings.py              (read-only settings inspector)
│       └── components/
│           ├── status_strip.py          (9 nodes, color-coded by state)
│           ├── diff_view.py             (difflib HtmlDiff with dark theme)
│           ├── branch_tree.py           (parent-run tree visualization)
│           ├── pass_card.py             (per-pass status + scrubbable timing)
│           ├── score_chips.py           (Pass-4 quality-score chip row)
│           └── session_drawer.py        (history + branch navigation drawer)
├── tests/
│   ├── conftest.py                      (FakeChatProvider + event_collector)
│   ├── test_concurrency.py              (the three load-bearing regression guards)
│   ├── test_parsing.py                  (27 tests — clamp, parsers, disambig Q&A)
│   ├── test_pipeline_smoke.py
│   ├── test_migration.py
│   ├── test_disambiguation.py           (pause + resume + per-pass timing)
│   ├── test_api_rest.py                 (NEW — REST endpoints)
│   ├── test_cli_auto_resume.py          (NEW — CLI auto-resume after disambig)
│   └── test_discovery.py                (NEW — provider/model discovery)
├── tools/
│   ├── methodology_agent.py             (passive Stop-hook reviewer)
│   ├── migrate_jsonl_to_sqlite.py       (one-shot migration; idempotent)
│   └── reviews/                         (output dir for method-*.md)
└── packaging/
    ├── prompt-enhancer.spec             (PyInstaller)
    ├── entrypoint.py                    (windowed launcher into NiceGUI)
    └── installer.iss                    (Inno Setup wrapper)
```

## Concurrency invariants (frozen — see `docs/EXTRACTION_GOTCHAS.md`)

1. `pass1 = await ...; pass2 = await ...` — never `asyncio.gather`.
   Test: `test_pass1_pass2_serial` — asserts wall-time ≥ 2× per-call latency.
2. Pass 4 awaited BEFORE Magnitude/SoT begin streaming.
   Test: `test_pass4_awaited_before_magnitude` — asserts call timestamps.
3. Every `chat_stream` carries `idle_timeout=120` (provider default).
   Test: `test_idle_timeout_propagates`.

## How to run end-to-end

```cmd
cd C:\Users\Falki\prompt-enhancer
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev,ui]"
pytest                                           rem 37 green
enhancer models                                  rem verify LM Studio at 1234
enhancer enhance "make me a chatbot" --model gptoss-120b-uncensored-hauhaucs-aggressive
enhancer ui                                      rem opens NiceGUI Studio at 127.0.0.1:8765
python tools\migrate_jsonl_to_sqlite.py --source ..\swarm-agent-dev\agent_pipeline.log
```

## What's left for v1.0

1. **Live verification** against `gpt-oss-120b-uncensored-hauhaucs-aggressive`
   via LM Link — confirms the three concurrency invariants hold under
   real remote-GPU latency. ✅ Done 2026-04-28 (see runs above).
2. ~~**Templates page** — CRUD over `templates` table; ship 8 seed templates.~~
   ✅ **Shipped** in `src/enhancer/ui/pages/templates.py`.
3. ~~**Compare page** in the UI — visual side-by-side scorecard.~~
   ✅ **Shipped** in `src/enhancer/ui/pages/compare.py`.
4. ~~**Branching from any pass** — schema supports it (`parent_run_id` +
   `parent_pass`); UI gesture is v0.2.~~
   ✅ **Shipped** 2026-05-02 (commit `f703012`). `PipelineOptions.branch_from_pass`
   + `parent_run_id` reuse parent's pass1/pass2/pass3 outputs; "↗ Branch from
   here" button on completed `pass_card`s; status-strip badge while branch
   streams; History row-detail Pass-1/2/3 buttons. Re-uses `AGENT_STEP
   step="branch_start"` (no EventType v2 bump). Tests: `test_branching.py` (3).
5. ~~**PyInstaller build (Python 3.12)** — spec + Inno script in `packaging/`.
   Existing `dist/prompt-enhancer.exe` is from 2026-04-28 against Python 3.13;
   needs rebuild against the 3.12 dev venv before shipping.~~
   ✅ **Shipped** 2026-05-02. `dist/prompt-enhancer/prompt-enhancer.exe`
   rebuilt against Python 3.12 (240 MB, smoke=HTTP 200 at `127.0.0.1:8765`);
   Inno Setup 6.7.1 wrapped it into `release/prompt-enhancer-setup.exe`
   (74 MB). To rebuild: from repo root run `pyinstaller packaging/prompt-enhancer.spec --clean`
   then `iscc packaging/installer.iss`.
6. ~~**TOML settings file** — env vars work today; persisted-from-UI
   settings land in v0.2.~~
   ✅ **Shipped** 2026-05-02 (commit `f703012`). `config.load()` layers
   defaults < TOML < env; `config.save_settings()` writes
   `%APPDATA%\prompt-enhancer\settings.toml` with atomic rename + `.bak`
   recovery; Settings page exposes 8 editable + 5 read-only keys; `POST
   /api/settings` validates types against the `Settings` dataclass. Tests:
   `test_config_toml.py` (3).

## Methodology Enhancement Agent — operating contract

* Live runner: `tools/methodology_agent.py` — reads `git diff --staged`
  (or `HEAD`) and POSTs a templated review prompt to LM Studio.
* Output: `tools/reviews/method-YYYYMMDD-HHMMSS.md`. Never raises.
* Adds <1 s to turn time. Switchable via `ENHANCER_METHODOLOGY_AGENT_ENABLED=0`.
* Wire it into Claude Code by adding to `~/.claude/settings.local.json`:
  ```json
  "hooks": {
    "Stop": [
      "python C:/Users/Falki/prompt-enhancer/tools/methodology_agent.py"
    ]
  }
  ```
* Architectural directives the agent enforces in every review:
  1. Pass 1 → Pass 2 strictly serial.
  2. Pass 4 awaited before Magnitude/SoT.
  3. `idle_timeout=120` on every `chat_stream` call.
  4. `EventType` enum + payload schema is FROZEN — bump v2 on change.
  5. `ChatProvider` ABC must not leak transport details.
  6. JSONL log format byte-for-byte matches the source monolith.
  7. `scores_fallback` and `pass3_partial` are public-contract flags.
