"""Session continuity — SQLite-backed.

Mirrors the API surface of the monolith's session helpers
(``_session_create / _list / _load / _rename / _clear / _delete /
_get_active``) so the standalone CLI and UI can wire to it without any
behaviour drift.

Session-context construction (newest-first, full-most-recent + 300-char
summaries) is preserved verbatim in :func:`build_context`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import connect


@dataclass
class SessionSummary:
    id: str
    name: str
    created_at: str
    updated_at: str
    entry_count: int
    is_active: bool


# ── active session is tracked in a single-row table for atomicity ───
# To avoid schema churn for v1 we keep "active" implicit: most-recently-
# updated session is the active one. v1.1 may add an explicit pointer.


def create(db_path: Path, name: str = "") -> SessionSummary:
    sid = secrets.token_hex(8)
    now = datetime.now().isoformat()
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "INSERT INTO sessions (id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, name or "Untitled Session", now, now),
            )
    return SessionSummary(
        id=sid, name=name or "Untitled Session",
        created_at=now, updated_at=now, entry_count=0, is_active=True,
    )


def list_all(db_path: Path, active_id: str | None = None) -> list[SessionSummary]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.created_at, s.updated_at,
                   COUNT(r.id) AS entry_count
            FROM sessions s
            LEFT JOIN runs r ON r.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
    return [
        SessionSummary(
            id=r["id"], name=r["name"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            entry_count=r["entry_count"], is_active=(r["id"] == active_id),
        )
        for r in rows
    ]


def get(db_path: Path, sid: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, created_at, updated_at FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        entries = [
            dict(r) for r in conn.execute(
                "SELECT id AS run_id, ts, prompt AS original_prompt, "
                "enhanced_prompt, magnitude_output "
                "FROM runs WHERE session_id = ? ORDER BY ts ASC",
                (sid,),
            ).fetchall()
        ]
    return {**dict(row), "entries": entries}


def rename(db_path: Path, sid: str, name: str) -> bool:
    if not name.strip():
        return False
    now = datetime.now().isoformat()
    with connect(db_path) as conn:
        with conn:
            cur = conn.execute(
                "UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
                (name.strip(), now, sid),
            )
    return cur.rowcount > 0


def delete(db_path: Path, sid: str) -> bool:
    with connect(db_path) as conn:
        with conn:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    return cur.rowcount > 0


def touch(db_path: Path, sid: str) -> None:
    """Bump updated_at — call when an entry is added so it's "active"."""
    now = datetime.now().isoformat()
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, sid),
            )


def build_context(session: dict[str, Any], token_budget: int = 3000) -> str:
    """Newest-first context block, most-recent in full, older as summaries.

    Preserves the algorithm at agent_pipeline.py:582-626. ``token_budget``
    is in tokens; we estimate at ~4 chars/token.
    """
    entries: list[dict[str, Any]] = session.get("entries", [])
    if not entries:
        return ""

    char_budget = token_budget * 4
    blocks: list[str] = []
    used = 0

    for i, entry in enumerate(reversed(entries)):
        is_most_recent = i == 0
        original = entry.get("original_prompt", "")
        enhanced = entry.get("enhanced_prompt", "")
        seq = entry.get("seq", i + 1)

        if is_most_recent:
            block = (
                f"--- Enhancement #{seq} (most recent) ---\n"
                f"Request: {original}\n"
                f"Enhanced specification:\n{enhanced}\n"
            )
        else:
            preview = enhanced[:300] + ("..." if len(enhanced) > 300 else "")
            block = (
                f"--- Enhancement #{seq} ---\n"
                f"Request: {original}\n"
                f"Enhanced (summary): {preview}\n"
            )

        if used + len(block) > char_budget:
            if is_most_recent:
                remaining = char_budget - used
                blocks.append(block[:remaining] + "\n[truncated]")
            break

        blocks.append(block)
        used += len(block)

    blocks.reverse()
    return "\n".join(blocks)
