"""SQLite connection + schema bootstrap.

Single-process app; WAL + busy_timeout cover the rare contention case
when CLI and UI run side-by-side. Schema lives in ``schema.sql`` and is
applied idempotently on first connection.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# ``importlib.resources`` is the right way to read package data, but for
# a developer install we also handle the ``schema.sql`` sibling-file case.
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + busy_timeout + row factory."""
    conn = sqlite3.connect(
        str(db_path),
        timeout=5.0,
        isolation_level=None,  # autocommit; explicit BEGIN/COMMIT in code
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    """Create the database file (if missing) and apply the schema.

    Also applies idempotent additive column migrations for pre-existing
    databases where the schema is older than the current source. Each
    migration is wrapped in a try/except so re-applying on an already-
    migrated DB is a no-op (SQLite raises ``OperationalError`` when a
    column already exists).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn = _connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Additive column migrations — idempotent; pre-v2.0.x DBs lack
        # the ``persona_partner`` column added in 2026-05.
        for stmt in (
            "ALTER TABLE runs ADD COLUMN persona_partner TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                # Column already exists — fine.
                pass
    finally:
        conn.close()


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Ensures the schema is applied first."""
    if not db_path.exists():
        init_db(db_path)
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
