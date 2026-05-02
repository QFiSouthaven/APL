# Migration

Two migration paths are documented here:

1. **Existing swarm-agent-dev users** importing their `agent_pipeline.log`
   into the standalone's SQLite.
2. **Cross-version upgrades** within the standalone (v0.1 → v0.2 etc.).

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
