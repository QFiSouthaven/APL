# prompt-enhancer

Local Desktop Studio for multi-pass AI prompt enhancement. LM Studio first; Ollama / OpenAI / Anthropic pluggable. Single-process Python; CLI and NiceGUI Studio share a transport-agnostic core.

## Project status

Per `STATUS.md`, phases 0–7 are claimed done: 4-pass pipeline, SQLite persistence, ChatProvider abstraction, typer CLI, NiceGUI Studio, packaging scaffolded, live-tested 2026-04-28 against gpt-oss-120b.

**Trust `STATUS.md` only after verifying against `git log` and `pytest -q`.** STATUS.md has drifted in the past — `api/`, `ui/pages/templates.py`, and `ui/pages/compare.py` shipped while STATUS.md still listed them as v0.2; the test count claim has lagged disk multiple times. As of v1.0.0 (2026-05-03) the suite is **85 tests in 12 files**. When in doubt, read `src/` and `tests/` directly.

## Frozen pipeline invariants

`src/enhancer/core/pipeline.py` carries three regression-guarded rules. Do not change them without bumping the `EventType` enum to v2 and updating the regression tests.

1. **Pass 1 → Pass 2 are STRICTLY SERIAL.** Never `asyncio.gather`. Test: `tests/test_concurrency.py::test_pass1_pass2_serial` (asserts wall-time ≥ 2× per-call latency).
2. **Pass 4 is awaited BEFORE Magnitude/SoT begin streaming.** Test: `test_pass4_awaited_before_magnitude` (asserts call timestamps).
3. **Every `provider.chat_stream` call uses `idle_timeout=120`** — the provider default. Test: `test_idle_timeout_propagates`.

Read `docs/EXTRACTION_GOTCHAS.md` before touching `pipeline.py`.

## Methodology Enhancement Agent

`tools/methodology_agent.py` is a passive Stop-hook reviewer: reads `git diff --staged` (or `HEAD`), POSTs a templated review prompt to LM Studio, and writes `tools/reviews/method-YYYYMMDD-HHMMSS.md`. Never raises; adds <1s to turn time. Toggle off with `ENHANCER_METHODOLOGY_AGENT_ENABLED=0`.

After a repo move, the hook in `~/.claude/settings.local.json` must be repointed to the new absolute path:

```json
"hooks": { "Stop": ["python C:/Users/Falki/APL/prompt-enhancer/tools/methodology_agent.py"] }
```

**Health check:** if `tools/reviews/` has no recent files after edits, the hook is dead — fix the path before trusting any review claim.

## Source layout

```
src/enhancer/
  core/         pipeline, passes, transforms, parsing, budgeting, events
  llm/          ChatProvider ABC + lmstudio (full, retry-wrapped) + ollama/openai/anthropic stubs
                + lms_link (base-URL override) + lms_discovery (auto-load via `lms` CLI)
                + resilience (@with_retry, @with_stream_retry, ProviderHealth circuit breaker)
  persistence/  SQLite (schema.sql, db, runs, sessions) + JSONL dual-writer + safestorage
  api/          REST + inter-product discovery (services.toml)
  cli/          typer entrypoint (main, enhance pre-flights ensure_model_loaded) + extras (batch / compare / export)
  ui/           NiceGUI Studio: app, pages/{studio,history,analytics,compare,templates,settings}, components/
                (session_drawer surfaces resilience.get_session_stats())
tests/          12 files: test_api_rest, test_branching, test_cli_auto_resume, test_concurrency,
                test_config_toml, test_disambiguation, test_discovery, test_lms_discovery,
                test_migration, test_parsing, test_pipeline_smoke, test_resilience (+ conftest)
packaging/      PyInstaller spec + Inno Setup script; dist/ has built exe; release/ has signed installer
tools/          methodology_agent.py (Stop-hook reviewer; pre-flights ensure_model_loaded) + migrate_jsonl_to_sqlite.py
```

## Entry points

- **CLI:** `enhancer` (typer) — subcommands: `enhance`, `models`, `history`, `ui`, `batch`, `compare`, `export`, `version`
- **UI:** `enhancer ui` → NiceGUI at `http://127.0.0.1:8765`
- **Bundled:** `packaging/dist/prompt-enhancer/prompt-enhancer.exe` (windowed launcher into the UI)

## Common commands

```bash
pip install -e ".[dev,ui]"                              # set up dev environment
pytest -q                                               # run tests
enhancer enhance "your prompt" --skip-clarify           # one-shot CLI run
enhancer ui                                             # launch Desktop Studio
pyinstaller packaging/prompt-enhancer.spec --clean      # rebuild bundled exe
iscc packaging/installer.iss                            # wrap exe → release/prompt-enhancer-setup.exe
```
