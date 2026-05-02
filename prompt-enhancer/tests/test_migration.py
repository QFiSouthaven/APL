"""JSONL → SQLite migration test.

Asserts:
1. A representative JSONL log copies into SQLite without loss.
2. Re-running is idempotent (no duplicate rows).
3. Dry-run inserts nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make tools/ importable as if it were a package for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from migrate_jsonl_to_sqlite import migrate  # noqa: E402

from enhancer.persistence.db import connect  # noqa: E402


SAMPLE_ENTRIES = [
    {
        "ts": "2026-01-01T10:00:00",
        "prompt": "Make a chatbot",
        "technique": "precision",
        "intent_preview": "GOAL: build chatbot",
        "weakness_preview": "VAGUE TERMS: none",
        "enhanced_preview": "Build a chatbot for customer support, ...",
        "task_type": "instructional",
        "scores": {"specificity": 8, "constraints": 7,
                   "actionability": 9, "improvement": 60},
        "pass_times_ms": {"pass1": 1000, "pass2": 1100, "pass3": 3000, "pass4": 800},
        "model": "fake-model-v1",
    },
    {
        "ts": "2026-01-02T11:00:00",
        "prompt": "Write a story",
        "technique": "context",
        "intent_preview": "GOAL: short story",
        "weakness_preview": "MISSING CONTEXT: audience",
        "enhanced_preview": "Write a short story about ...",
        "task_type": "creative",
        "model": "fake-model-v2",
    },
]


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_migration_inserts_all(tmp_path: Path):
    src = tmp_path / "agent_pipeline.log"
    db = tmp_path / "enhancer.db"
    _write_jsonl(src, SAMPLE_ENTRIES)

    stats = migrate(src, db)
    assert stats["read"] == 2
    assert stats["inserted"] == 2
    assert stats["skipped_existing"] == 0

    with connect(db) as conn:
        rows = conn.execute("SELECT * FROM runs").fetchall()
        scores = conn.execute("SELECT * FROM scores").fetchall()
    assert len(rows) == 2
    assert len(scores) == 1  # only the first entry had scores


def test_migration_is_idempotent(tmp_path: Path):
    src = tmp_path / "agent_pipeline.log"
    db = tmp_path / "enhancer.db"
    _write_jsonl(src, SAMPLE_ENTRIES)

    migrate(src, db)
    stats = migrate(src, db)
    assert stats["inserted"] == 0
    assert stats["skipped_existing"] == 2


def test_migration_dry_run(tmp_path: Path):
    src = tmp_path / "agent_pipeline.log"
    db = tmp_path / "enhancer.db"
    _write_jsonl(src, SAMPLE_ENTRIES)

    stats = migrate(src, db, dry_run=True)
    assert stats["inserted"] == 2
    with connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()
    assert rows["c"] == 0


def test_migration_skips_invalid_lines(tmp_path: Path):
    src = tmp_path / "agent_pipeline.log"
    db = tmp_path / "enhancer.db"
    src.write_text(
        json.dumps(SAMPLE_ENTRIES[0]) + "\nthis is not json\n"
        + json.dumps(SAMPLE_ENTRIES[1]) + "\n",
        encoding="utf-8",
    )
    stats = migrate(src, db)
    assert stats["inserted"] == 2
    assert stats["skipped_invalid"] == 1
