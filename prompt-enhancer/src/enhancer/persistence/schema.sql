-- Prompt Enhancer SQLite schema (v1).
--
-- Migration policy: column additions only. Renaming or dropping requires
-- a v2 schema file + migration script in tools/.
--
-- WAL mode + busy_timeout=5000 are set in code (db.py).

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    context_budget  INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    parent_run_id       TEXT REFERENCES runs(id) ON DELETE SET NULL,
    parent_pass         INTEGER,                                -- which pass we forked from
    ts                  TEXT NOT NULL,
    prompt              TEXT NOT NULL,
    enhanced_prompt     TEXT NOT NULL,
    task_type           TEXT,
    technique           TEXT,
    persona             TEXT,
    pass1_output        TEXT,
    pass2_output        TEXT,
    pass4_output        TEXT,
    magnitude_output    TEXT,
    sot_output          TEXT,
    pass_times_ms_json  TEXT,
    model               TEXT,
    scorer_model        TEXT,
    temperature         REAL,
    max_tokens_scale    REAL,
    scores_fallback     INTEGER NOT NULL DEFAULT 0,
    pass3_partial       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scores (
    run_id          TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    specificity     INTEGER,
    constraints     INTEGER,
    actionability   INTEGER,
    improvement     INTEGER
);

CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    domain      TEXT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    source      TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_session_ts
    ON runs(session_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_runs_task_type
    ON runs(task_type);

-- ``improvement`` lives in the ``scores`` table; index there.
CREATE INDEX IF NOT EXISTS idx_scores_improvement
    ON scores(improvement);

CREATE INDEX IF NOT EXISTS idx_runs_parent
    ON runs(parent_run_id)
    WHERE parent_run_id IS NOT NULL;
