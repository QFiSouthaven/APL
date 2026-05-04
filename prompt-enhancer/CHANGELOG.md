# Changelog

All notable changes to **prompt-enhancer** are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned for v0.2
- Multi-host LM Studio routing (port `lms_link.py` from monolith).
- Task-aware scorer auto-selection (port `model_router.py`).
- Persisted TOML settings (replace env-var-only configuration).
- PyInstaller signed installer with EV cert.
- MCP tool integration for Pass 1/3 enrichment.

---

## [0.1.0] — 2026-04-28 — first standalone release

### Added — core
- Four-pass enhancer (Intent / Weakness / Rewrite / Score) extracted
  from the swarm-agent-dev WebUI monolith into a stand-alone Python
  package.
- Optional Persona, Magnitude, and Skeleton-of-Thought transforms.
- Interactive disambiguation pause/resume — pipeline pauses when Pass 2
  detects ≥ 3 weakness fields and asks the user 2-3 multiple-choice
  clarification questions.
- Self-correction retry loop when Pass 4 reports `improvement < 20%`.
- Smart truncation (first 20% + last 80%) preserves tail instructions
  on long prompts.
- Adaptive context-budget detection via LM Studio management API
  (`/api/v0/models`) with model-name regex + parameter-count fallback.
- `_clamp(value, lo, hi, default)` guard for any user-supplied numeric.
- 30-member frozen `EventType` enum forms the standalone's API boundary.

### Added — providers
- `ChatProvider` ABC defining `list_models`, `chat`, `chat_stream`,
  optional `context_window`.
- `LMStudioProvider` — full v1 implementation with `idle_timeout=120`
  protection against silent stream stalls.
- `OllamaProvider`, `OpenAIProvider`, `AnthropicProvider` — stubs with
  helpful "install with `pip install prompt-enhancer[<extra>]`" hints
  for v1.1.

### Added — persistence
- SQLite schema (`sessions`, `runs`, `scores`, `templates`) under
  `%APPDATA%\prompt-enhancer\enhancer.db`.
- `runs.save()` dual-writes SQLite + JSONL during a one-release
  migration window so `swarm-agent-dev/devflow.py` keeps consuming the
  monolith's `agent_pipeline.log`.
- `tools/migrate_jsonl_to_sqlite.py` — idempotent migration with
  dry-run mode; skips invalid lines.
- `SafeStorage` atomic-write helper (ported from `src/core/system.py`
  in the monolith).

### Added — CLI (`enhancer ...`)
- `version`, `models`, `enhance`, `history`, `ui`, `batch`, `compare`,
  `export` (clipboard / md / curl / json).
- `--skip-clarify` flag for non-interactive disambiguation skip.
- Interactive `typer.prompt()` Q&A loop on disambiguation pause.
- Windows UTF-8 stdout reconfiguration so cp1252 can't crash on
  smart quotes / em dashes / non-breaking hyphens emitted by LLMs.

### Added — UI (NiceGUI Desktop Studio)
- Six routed pages — Studio, History, Analytics, Compare, Templates,
  Settings.
- Status strip with 9 nodes + live token-count / elapsed / tok-per-sec
  progress line under the strip (polled every 0.5 s).
- Per-pass result cards with score chips (specificity / constraints /
  actionability / improvement %).
- Original ↔ Enhanced diff view via `difflib.HtmlDiff` with dark theme.
- Disambiguation modal with radio-button Q&A.
- Session drawer (right-side overlay) for create / switch / rename /
  delete.
- Branch tree component on the run-detail panel — visualizes
  parent_run_id / parent_pass lineage.
- Templates library — CRUD over the `templates` table; ships with 8
  seed templates (coding, creative, analytical, instructional,
  conversational, factual, pre-mortem, persona-letter).
- Analytics page — KPI cards plus echarts pie + bar visualizations.

### Added — packaging
- `Start.bat` — idempotent launcher: creates `.venv` on first run,
  installs `[ui]` extras, sanity-checks LM Studio at 1234, launches
  Studio at 127.0.0.1:8765.
- `Stop.bat` — kills any process listening on the Studio port.
- PyInstaller spec at `packaging/prompt-enhancer.spec` for single-folder
  distributable; Inno Setup script at `packaging/installer.iss` wraps
  it into a Windows installer.

### Added — tests (45+)
- `test_concurrency.py` — three load-bearing regression guards:
  serial Pass 1 → Pass 2, Pass 4 awaited before Magnitude/SoT, idle
  timeout propagation.
- `test_disambiguation.py` — pause + resume + per-pass timing +
  skip-clarify path.
- `test_pipeline_smoke.py` — minimal pipeline + persona/magnitude +
  Pass 3 fallback.
- `test_parsing.py` — every parse helper + `_clamp` + the
  "instructional + code → coding" override.
- `test_migration.py` — JSONL → SQLite migration idempotence + dry-run.
- `test_cli_auto_resume.py` — `_run_with_auto_resume` helper for the
  `compare` and `batch` subcommands.

### Added — documentation
- `STATUS.md` — phase-by-phase status with live-test results.
- `docs/EXTRACTION_GOTCHAS.md` — methodology-agent guard rail
  cataloguing every coupling site, order-dependent state mutation, and
  the three concurrency invariants.
- Architecture, providers, events, migration, plugins docs under
  `docs/` (this release).
- `tools/methodology_agent.py` — passive review hook script callable
  from Claude Code's `Stop` event.

### Fixed
- Pass 4 returned empty content for reasoning-token models like
  gpt-oss-120b → switched to streaming + bumped `gen_score` budget
  200 → 400. Live-verified `scores_fallback=0` on
  `gptoss-120b-uncensored-hauhaucs-aggressive`.
- Persona detection had the same gpt-oss issue → also switched to
  streaming.
- Pass 1 / Pass 2 `duration_ms` was averaging both passes →
  now tracks per-pass independently.
- `compare` and `batch` silently returned `P4_DEFAULTS` on vague
  prompts → both now auto-resume the disambiguation pause via
  `_run_with_auto_resume`.
- Windows console crashed on Unicode glyphs in LLM output →
  `cli/main.py` reconfigures stdout to UTF-8 with replacement.
- NiceGUI 3.11 doesn't allow `ui.add_head_html` at module scope when
  using `@ui.page` → moved into a per-route helper.

### Known limitations
- Single-host LM Studio (multi-host LAN routing planned for v0.2).
- Sessions persistence requires SQLite (`enhancer.db`); no remote sync.
- Settings UI is read-only — env vars are the v0.1 control surface.

[Unreleased]: https://github.com/QFiSouthaven/APL/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/QFiSouthaven/APL/releases/tag/v0.1.0
