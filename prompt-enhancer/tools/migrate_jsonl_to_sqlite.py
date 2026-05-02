#!/usr/bin/env python3
"""One-time migration: ``agent_pipeline.log`` (JSONL) → SQLite ``runs`` table.

Usage::

    python tools/migrate_jsonl_to_sqlite.py [--source PATH] [--db PATH] [--dry-run]

Defaults read from ``ENHANCER_*`` env vars / platformdirs (the same values
the running app uses). The source JSONL is **not modified**: rows are
copied into SQLite only.

Idempotent: re-running skips runs whose generated id already exists. Run
ids are derived from ``ts + prompt[:100]`` so the same JSONL line maps to
the same SQLite row across runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

# Make the package importable when running as a script from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from enhancer.config import db_path as default_db_path  # noqa: E402
from enhancer.config import jsonl_log_path as default_jsonl_path  # noqa: E402
from enhancer.persistence.db import connect, init_db  # noqa: E402


def _stable_id(entry: dict) -> str:
    """Deterministic 16-hex id from ts + first 100 chars of prompt."""
    seed = f"{entry.get('ts', '')}|{entry.get('prompt', '')[:100]}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def migrate(source: Path, db: Path, *, dry_run: bool = False) -> dict[str, int]:
    if not source.exists():
        return {"read": 0, "inserted": 0, "skipped_existing": 0, "skipped_invalid": 0}

    init_db(db)
    stats = {"read": 0, "inserted": 0, "skipped_existing": 0, "skipped_invalid": 0}

    with source.open("r", encoding="utf-8") as f, connect(db) as conn:
        existing = {
            r["id"] for r in conn.execute("SELECT id FROM runs").fetchall()
        }
        for line in f:
            stats["read"] += 1
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                stats["skipped_invalid"] += 1
                continue

            run_id = _stable_id(entry)
            if run_id in existing:
                stats["skipped_existing"] += 1
                continue

            ts = entry.get("ts") or datetime.now().isoformat()
            prompt = entry.get("prompt", "")
            enhanced = entry.get("enhanced_preview", "")
            scores = entry.get("scores") or {}
            if dry_run:
                stats["inserted"] += 1
                continue

            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runs (
                        id, ts, prompt, enhanced_prompt,
                        task_type, technique, persona,
                        pass1_output, pass2_output, pass4_output,
                        pass_times_ms_json,
                        model, scorer_model,
                        temperature, max_tokens_scale,
                        scores_fallback, pass3_partial
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        run_id, ts, prompt, enhanced,
                        entry.get("task_type"),
                        entry.get("technique"),
                        entry.get("persona"),
                        entry.get("pass1_output"),
                        entry.get("pass2_output"),
                        entry.get("pass4_output"),
                        json.dumps(entry.get("pass_times_ms")) if entry.get("pass_times_ms") else None,
                        entry.get("model"),
                        entry.get("scorer_model"),
                        0.7, 1.0,  # historical default knobs
                        0, 0,
                    ),
                )
                if scores:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO scores
                            (run_id, specificity, constraints, actionability, improvement)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            scores.get("specificity"),
                            scores.get("constraints"),
                            scores.get("actionability"),
                            scores.get("improvement"),
                        ),
                    )
            existing.add(run_id)
            stats["inserted"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=None,
                    help="Path to agent_pipeline.log (defaults to user data dir)")
    ap.add_argument("--db", type=Path, default=None,
                    help="Path to enhancer.db (defaults to user data dir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Count without writing to SQLite")
    args = ap.parse_args()

    source = args.source or default_jsonl_path()
    db = args.db or default_db_path()

    print(f"Source: {source}")
    print(f"DB:     {db}")
    if args.dry_run:
        print("(dry-run mode — no writes)")

    if not source.exists():
        print("Source file does not exist; nothing to migrate.")
        return 0

    stats = migrate(source, db, dry_run=args.dry_run)
    print(
        f"\nRead:             {stats['read']}\n"
        f"Inserted:         {stats['inserted']}\n"
        f"Skipped existing: {stats['skipped_existing']}\n"
        f"Skipped invalid:  {stats['skipped_invalid']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
