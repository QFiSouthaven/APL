# Migration

Three migration tracks are documented here:

1. **v1.x → v2.0** — the capstone release (this is the section most readers
   want; jump to [§ v1 → v2](#v1--v20-the-capstone-release)).
2. **Existing swarm-agent-dev users** importing their `agent_pipeline.log`
   into the standalone's SQLite.
3. **Cross-version upgrades** within the standalone (v0.1 → v0.2 etc.).

---

## v1 → v2.0 (the capstone release)

### Versioning model

prompt-enhancer follows [SemVer](https://semver.org/spec/v2.0.0.html) with
an **additive interpretation**: a major bump never deletes a v1 event name
or breaks a v1 SQLite column on day one of v2. Most v2.0 changes ARE
additive — new events, new optional config keys, new entry-point groups —
yet we cut a major release because:

- TOML-driven pipeline configurability and MCP tool invocation are a
  **conceptual surface expansion**, not just bugfixes. Integrators reading
  the changelog should see "major" and budget time to re-test, not skim
  a minor and discover later that their dashboard is missing six event
  rows.
- A major version is the only signal that scales — minor releases are too
  noisy in a project this active to carry that meaning.
- Aligns with the contract in [`docs/EVENTS.md`](EVENTS.md) §
  "Removing or renaming": "Renaming or repurposing an existing event is
  a v2 migration." We are not renaming anything in v2.0, but we are
  expanding the catalog enough that consumers should re-derive it.

### What's new in v2.0

- **Six new `EventType` members** (full table in
  [§ EventType v2 reference table](#eventtype-v20-reference-table)):
  - `PROVIDER_HEALTH_OPEN` / `PROVIDER_HEALTH_CLOSED` — circuit-breaker
    observability over the resilience layer landed in v1.1 (phase 9).
  - `MCP_TOOL_INVOKED` / `MCP_TOOL_RESULT` — emitted around MCP tool
    calls in Pass 1 / Pass 3.
  - `BRANCHING_FORK` — replaces the v1 ad-hoc `AGENT_STEP step="branch_start"`
    pattern with a typed event. The v1 form is retained for one release.
  - `BRANCHING_MERGE` — reserved for v2.x; the enum value exists today
    but is not yet emitted.
- **MCP tool invocation in Pass 1 / Pass 3** — Pass 1 may consult
  read-only MCP servers for context enrichment; Pass 3 may call
  registered tools to ground the rewrite. See `docs/PIPELINE_GRAPH.md`.
- **TOML pipeline graph configurability** — the four-pass pipeline +
  optional transforms is now describable as a TOML graph. Defaults
  reproduce v1 behavior byte-for-byte; custom graphs unlock new
  topologies without forking `pipeline.py`.
- **`enhancer.transforms` entry-point group** — third-party packages
  register transforms (alongside the existing `enhancer.providers`
  group from v1.2 phase 14) without patching `transforms.py`.
- **UI test infrastructure** — Studio now has its first automated test
  surface so component regressions catch in CI rather than at release.

### What changed (NOT broken)

_Empty for v2.0._ This section is reserved for future releases that
modify a v1 surface in a backward-compatible way (e.g., relaxing a
required-field rule, broadening an enum). Nothing in v1 has been touched.

### What's deprecated

_Empty for v2.0._ Future deprecations will be announced **one minor
release ahead** of removal per the
[compatibility commitment](#compatibility-commitment).

### Migration steps for consumers

#### Code consuming the JSONL stream

Nothing to change for v1 events. Existing readers that switch on
`event` keys against the 30 v1 names keep working unchanged. To opt
in to the new event surface:

- Add cases for the six new event names — see the table below for
  payload shapes.
- For `BRANCHING_FORK`: if you previously matched against
  `event=="agent_step"` with `step=="branch_start"`, prefer the typed
  event going forward; the v1 emission is retained for one release
  and removable in v3.

#### Code calling `get_provider("xxx")`

In v1.2 (phase 14) the registry began consulting the
`enhancer.providers` entry-point group. Third-party providers should
move from monkey-patching `enhancer/llm/registry.py` to declaring an
entry point:

```toml
# pyproject.toml of the third-party package
[project.entry-points."enhancer.providers"]
my_provider = "my_pkg.provider:MyProvider"
```

See [`docs/PLUGINS.md`](PLUGINS.md) for the full contract. v2.0
preserves the in-tree provider list (`lmstudio`, `ollama`, `openai`,
`anthropic`).

#### Code writing TOML pipeline configs

The schema is documented in
[`docs/PIPELINE_GRAPH.md`](PIPELINE_GRAPH.md) (lands with the v2.0
capstone). MIGRATION.md cross-references that document until the two
can be consolidated. The default graph reproduces the v1 pipeline
byte-for-byte; you only need a TOML file if you want a non-default
topology.

#### Code customizing UI components

> **FIXME — harness pinned by v2.0 UI testing work.** The UI test
> harness (likely `nicegui.testing` or a thin wrapper) is being
> finalized by the parallel UI agent. This section will be filled in
> once the call is made; for now, custom UI components written against
> v1.x continue to work without modification.

### TOML pipeline graph schema

Schema documented in
[`docs/PIPELINE_GRAPH.md`](PIPELINE_GRAPH.md) (lands with v2.0
capstone). Cross-reference until consolidated. Highlights:

- TOML lives at `%APPDATA%\prompt-enhancer\pipeline.toml` (Windows) or
  `~/.config/prompt-enhancer/pipeline.toml` (Linux/macOS), layered on
  top of the bundled defaults.
- Identical concurrency invariants apply to any TOML-defined graph —
  the runtime enforces serial Pass 1 → Pass 2 and Pass 4 awaited
  before transforms regardless of graph topology.

### EventType v2.0 reference table

Canonical reference for the 36-member enum. Grouped by semantic family
to match the source ordering in
[`src/enhancer/core/events.py`](../src/enhancer/core/events.py).

#### v1.x members (frozen — retained in v2.x)

| Name | Value | Group | Since |
|---|---|---|---|
| `AGENT_STEP` | `agent_step` | pipeline backbone | v0.1 |
| `AGENT_PASS_START` | `agent_pass_start` | pipeline backbone | v0.1 |
| `AGENT_PASS_CHUNK` | `agent_pass_chunk` | pipeline backbone | v0.1 |
| `AGENT_PASS_RESULT` | `agent_pass_result` | pipeline backbone | v0.1 |
| `AGENT_PIPELINE_SUMMARY` | `agent_pipeline_summary` | pipeline backbone | v0.1 |
| `ENHANCEMENT_SCORE` | `enhancement_score` | pipeline backbone | v0.1 |
| `AGENT_DONE` | `agent_done` | pipeline backbone | v0.1 |
| `AGENT_ERROR` | `agent_error` | pipeline backbone | v0.1 |
| `AGENT_DISAMBIGUATE` | `agent_disambiguate` | disambiguation | v0.1 |
| `PERSONA_START` | `persona_start` | persona | v0.1 |
| `PERSONA_RESULT` | `persona_result` | persona | v0.1 |
| `MAGNITUDE_START` | `magnitude_start` | magnitude | v0.1 |
| `MAGNITUDE_CHUNK` | `magnitude_chunk` | magnitude | v0.1 |
| `MAGNITUDE_DONE` | `magnitude_done` | magnitude | v0.1 |
| `MAGNITUDE_ERROR` | `magnitude_error` | magnitude | v0.1 |
| `SOT_START` | `sot_start` | skeleton-of-thought | v0.1 |
| `SOT_CHUNK` | `sot_chunk` | skeleton-of-thought | v0.1 |
| `SOT_DONE` | `sot_done` | skeleton-of-thought | v0.1 |
| `SOT_ERROR` | `sot_error` | skeleton-of-thought | v0.1 |
| `PRETRIAL_START` | `pretrial_start` | pretrial | v0.1 |
| `PRETRIAL_RESULT` | `pretrial_result` | pretrial | v0.1 |
| `PRETRIAL_ERROR` | `pretrial_error` | pretrial | v0.1 |
| `SESSION_CREATED` | `session_created` | sessions | v0.1 |
| `SESSION_LIST` | `session_list` | sessions | v0.1 |
| `SESSION_LOADED` | `session_loaded` | sessions | v0.1 |
| `SESSION_RENAMED` | `session_renamed` | sessions | v0.1 |
| `SESSION_CLEARED` | `session_cleared` | sessions | v0.1 |
| `SESSION_DELETED` | `session_deleted` | sessions | v0.1 |
| `SESSION_ENTRY_ADDED` | `session_entry_added` | sessions | v0.1 |
| `SESSION_ACTIVE` | `session_active` | sessions | v0.1 |

#### v2.0 additions

| Name | Value | Group | Payload |
|---|---|---|---|
| `PROVIDER_HEALTH_OPEN` | `provider_health_open` | provider health | `{"provider": str, "consecutive_failures": int, "cooldown_secs": float}` |
| `PROVIDER_HEALTH_CLOSED` | `provider_health_closed` | provider health | `{"provider": str}` |
| `MCP_TOOL_INVOKED` | `mcp_tool_invoked` | MCP | `{"server": str, "tool": str, "args_summary": str}` |
| `MCP_TOOL_RESULT` | `mcp_tool_result` | MCP | `{"server": str, "tool": str, "ok": bool, "duration_ms": float, "error": str \| None}` |
| `BRANCHING_FORK` | `branching_fork` | branching | `{"parent_run_id": str, "parent_pass": int, "new_run_id": str}` |
| `BRANCHING_MERGE` | `branching_merge` | branching (reserved) | `{"source_run_ids": list[str], "merged_run_id": str}` (when wired) |

### Compatibility commitment

- **v2.x will continue to emit all v1 event names** under their existing
  spelling and string value.
- **v3.0 may remove deprecated members.** Deprecations will be announced
  **one minor release ahead** in this document and the changelog so
  integrators always have at least one minor-release window to migrate.
- The `EventType` enum is a `str`-mixin; JSONL stream values are stable
  across the v2 line.
- See [`docs/EVENTS.md`](EVENTS.md) for the live payload schemas and the
  authoritative list of public-contract flags (`scores_fallback`,
  `pass3_partial`).

---

## 1. JSONL → SQLite (one-time, for existing monolith users)

The standalone primary persistence is SQLite. Your existing
`C:\Users\Falki\swarm-agent-dev\agent_pipeline.log` (JSON-lines, one
entry per pipeline run) carries history that's worth keeping.

### Running the migration

```cmd
cd C:\Users\Falki\prompt-enhancer
.venv\Scripts\activate
python tools\migrate_jsonl_to_sqlite.py ^
       --source C:\Users\Falki\swarm-agent-dev\agent_pipeline.log
```

Defaults:
- Reads from `--source` (or the standalone's own
  `%APPDATA%\prompt-enhancer\agent_pipeline.log` if omitted).
- Writes to `--db` (or the standalone's `enhancer.db`).
- Use `--dry-run` to count rows without writing.

### Properties

- **Idempotent.** Re-running skips rows whose deterministic id
  (`sha1(ts | prompt[:100])[:16]`) already exists.
- **Read-only on source.** The JSONL file is never modified.
- **Best-effort field mapping.** Optional fields (`scores`,
  `pass_times_ms`, `persona`, etc.) are copied if present, omitted
  otherwise. Historical rows have no `scores_fallback` /
  `pass3_partial` flags — they default to `0`.
- **Skips invalid lines.** Malformed JSON lines are counted in
  `skipped_invalid` and not aborted on.

### Output

```
Source:           C:\Users\Falki\swarm-agent-dev\agent_pipeline.log
DB:               C:\Users\Falki\AppData\Local\prompt-enhancer\enhancer.db

Read:             N
Inserted:         M
Skipped existing: K
Skipped invalid:  L
```

After migration, `enhancer history` and the Studio's History page show
the imported rows alongside any new runs.

### Dual-write window

While the standalone is in v0.x, **`runs.save()` writes both SQLite AND
JSONL** so the monolith's `devflow.py` (which reads
`agent_pipeline.log` for default criteria) keeps working. Once you're
no longer using the monolith, you can:

1. Set the `ENHANCER_JSONL_DUAL_WRITE=0` env var (planned v0.2 toggle).
2. Or simply ignore the JSONL file — SQLite is canonical.

---

## 2. Cross-version upgrades

### v0.1 → v0.2 (planned)

Schema changes already accommodated by the v0.1 schema (forward-
compatible columns):

- `runs.parent_run_id`, `runs.parent_pass` — already present for
  branching gestures.
- `runs.magnitude_output`, `runs.sot_output` — already present.

Planned breaking-ish changes (will ship with a migration script):

- Add `runs.cost_estimate REAL` column for cost tracking.
- Add `runs.tokens_in INTEGER`, `runs.tokens_out INTEGER` columns for
  per-run token accounting.
- Persist Settings to `settings.toml` (currently env-var only).

### v0.x → v1.0 (planned)

- Freeze the SQLite schema — no breaking changes after v1.0.
- Drop the JSONL dual-writer (one-release deprecation window).
- `chain_events.py` consumers in the monolith will need an adapter
  that reads `enhancer.db` directly via `enhancer.persistence.runs`.

### v1.x → v2.0 (planned)

- Bump `EventType` namespace if the event contract changes.
- Compat layer emits v1 + v2 names for one release.
- See `docs/EVENTS.md` § "Removing or renaming".

---

## 3. Schema reference

```sql
sessions(id, name, created_at, updated_at, context_budget)

runs(id, session_id, parent_run_id, parent_pass,
     ts, prompt, enhanced_prompt,
     task_type, technique, persona,
     pass1_output, pass2_output, pass4_output,
     magnitude_output, sot_output,
     pass_times_ms_json,
     model, scorer_model,
     temperature, max_tokens_scale,
     scores_fallback, pass3_partial)

scores(run_id, specificity, constraints, actionability, improvement)

templates(id, domain, title, body, created_at, source)
```

Indexes: `idx_runs_session_ts`, `idx_runs_task_type`,
`idx_scores_improvement`, `idx_runs_parent`.

WAL mode + 5-second `busy_timeout` are set on every connection in
`persistence/db.py`.

---

## 4. Settings migration (planned v0.2)

Today the standalone reads only env vars (`ENHANCER_*`). v0.2 will
introduce a TOML settings file at:

- Windows: `%APPDATA%\prompt-enhancer\settings.toml`
- Linux/macOS: `~/.config/prompt-enhancer/settings.toml`

The file is layered **on top of** env vars (env wins), so existing
shell-config-driven setups keep working. The Studio's Settings page
will gain Save buttons that write the TOML file.

---

## 5. Rollback

The migration script never removes anything. To roll back:

1. Delete `%APPDATA%\prompt-enhancer\enhancer.db` (and the `-wal` and
   `-shm` files alongside it).
2. The monolith's JSONL log is untouched and continues to work as the
   source of truth.

For a deeper roll-forward (e.g., re-run a failed migration), add
`--dry-run` to the migration command first to preview row counts.
