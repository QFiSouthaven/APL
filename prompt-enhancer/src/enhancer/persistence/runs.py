"""Run CRUD — primary write path is :func:`save`, which dual-writes
SQLite + JSONL for one release for ``devflow.py`` compatibility.

Also serves the History / Analytics pages via :func:`list_recent`,
:func:`get_run`, :func:`stats`.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import connect
from .jsonl_compat import append as jsonl_append


@dataclass
class RunRecord:
    """One pipeline run persisted to SQLite (and tee'd to JSONL)."""

    prompt: str
    enhanced_prompt: str
    task_type: str = ""
    technique: str = "precision"
    persona: str | None = None
    persona_partner: str | None = None
    pass1_output: str = ""
    pass2_output: str = ""
    pass4_output: str = ""
    magnitude_output: str = ""
    sot_output: str = ""
    pass_times_ms: dict[str, int] = field(default_factory=dict)
    model: str = ""
    scorer_model: str = ""
    temperature: float = 0.7
    max_tokens_scale: float = 1.0
    scores: dict[str, int] = field(default_factory=dict)
    scores_fallback: bool = False
    pass3_partial: bool = False
    session_id: str | None = None
    parent_run_id: str | None = None
    parent_pass: int | None = None
    id: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", secrets.token_hex(8))
        if not self.ts:
            object.__setattr__(self, "ts", datetime.now().isoformat())


def save(record: RunRecord, db_path: Path, jsonl_path: Path | None = None) -> str:
    """Persist a run to SQLite and (optionally) tee to JSONL.

    The JSONL line matches ``agent_pipeline.py:_log_pipeline_run`` byte-
    for-byte so ``devflow.py`` and the existing analytics dashboard keep
    working during the migration window.
    """
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, session_id, parent_run_id, parent_pass,
                    ts, prompt, enhanced_prompt,
                    task_type, technique, persona, persona_partner,
                    pass1_output, pass2_output, pass4_output,
                    magnitude_output, sot_output,
                    pass_times_ms_json,
                    model, scorer_model,
                    temperature, max_tokens_scale,
                    scores_fallback, pass3_partial
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?,
                    ?, ?,
                    ?, ?,
                    ?, ?
                )
                """,
                (
                    record.id, record.session_id, record.parent_run_id, record.parent_pass,
                    record.ts, record.prompt, record.enhanced_prompt,
                    record.task_type or None, record.technique or None,
                    record.persona, record.persona_partner,
                    record.pass1_output or None, record.pass2_output or None,
                    record.pass4_output or None,
                    record.magnitude_output or None, record.sot_output or None,
                    json.dumps(record.pass_times_ms) if record.pass_times_ms else None,
                    record.model or None, record.scorer_model or None,
                    record.temperature, record.max_tokens_scale,
                    1 if record.scores_fallback else 0,
                    1 if record.pass3_partial else 0,
                ),
            )
            if record.scores:
                conn.execute(
                    """
                    INSERT INTO scores (run_id, specificity, constraints,
                                        actionability, improvement)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.scores.get("specificity"),
                        record.scores.get("constraints"),
                        record.scores.get("actionability"),
                        record.scores.get("improvement"),
                    ),
                )
    if jsonl_path is not None:
        jsonl_append(record, jsonl_path)
    return record.id


def list_recent(
    db_path: Path,
    *,
    limit: int = 20,
    task_type: str | None = None,
    min_improvement: int | None = None,
) -> list[dict[str, Any]]:
    """Recent runs joined with scores, newest-first."""
    sql = """
        SELECT r.*, s.specificity, s.constraints, s.actionability, s.improvement
        FROM runs r
        LEFT JOIN scores s ON s.run_id = r.id
        WHERE 1=1
    """
    params: list[Any] = []
    if task_type:
        sql += " AND r.task_type = ?"
        params.append(task_type)
    if min_improvement is not None:
        sql += " AND s.improvement >= ?"
        params.append(min_improvement)
    sql += " ORDER BY r.ts DESC LIMIT ?"
    params.append(limit)

    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_run(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT r.*, s.specificity, s.constraints, s.actionability, s.improvement
            FROM runs r
            LEFT JOIN scores s ON s.run_id = r.id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


def stats(db_path: Path) -> dict[str, Any]:
    """Aggregate counters for the analytics page."""
    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        techniques = {
            r["technique"]: r["c"]
            for r in conn.execute(
                "SELECT technique, COUNT(*) AS c FROM runs "
                "WHERE technique IS NOT NULL GROUP BY technique"
            )
        }
        task_types = {
            r["task_type"]: r["c"]
            for r in conn.execute(
                "SELECT task_type, COUNT(*) AS c FROM runs "
                "WHERE task_type IS NOT NULL GROUP BY task_type"
            )
        }
        avg_row = conn.execute(
            """
            SELECT AVG(specificity) AS specificity,
                   AVG(constraints) AS constraints,
                   AVG(actionability) AS actionability,
                   AVG(improvement) AS improvement
            FROM scores
            """
        ).fetchone()
        last_ts_row = conn.execute(
            "SELECT ts FROM runs ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return {
        "total_runs": total,
        "techniques": techniques,
        "task_types": task_types,
        "average_scores": {k: avg_row[k] for k in
                           ("specificity", "constraints", "actionability", "improvement")
                           } if avg_row else {},
        "last_ts": last_ts_row["ts"] if last_ts_row else None,
    }
