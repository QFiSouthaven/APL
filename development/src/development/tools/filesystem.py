"""Read-only filesystem tools, scoped to a sandbox dir.

Both functions reject any input that resolves outside ``sandbox_dir`` —
that's the security boundary. We resolve via ``Path.resolve()`` (which
collapses ``..`` and symlinks) and check that the resolved path is still
under ``sandbox_dir.resolve()``.

Return shape: ``{ok: bool, ...}``. ``ok=False`` always carries an
``error`` string; the LLM sees it and can adapt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _resolve_inside(sandbox_dir: Any, path: str) -> Path | None:
    """Resolve ``path`` relative to ``sandbox_dir`` and ensure it stays inside.

    Returns the resolved :class:`Path` on success, ``None`` if the input
    escapes the sandbox or can't be resolved.
    """
    if sandbox_dir is None:
        return None
    sandbox_root = Path(sandbox_dir).resolve()
    p = Path(path) if path else Path(".")
    if p.is_absolute():
        return None
    try:
        resolved = (sandbox_root / p).resolve()
    except (OSError, ValueError):
        return None
    try:
        resolved.relative_to(sandbox_root)
    except ValueError:
        return None
    return resolved


async def fs_read(
    path: str,
    *,
    sandbox_dir: Any,
    max_bytes: int = 65536,
) -> dict[str, Any]:
    """Read a text file from the sandbox dir.

    Returns ``{ok, content_or_error, bytes_read, truncated}``. Errors
    (path outside sandbox, file missing, decode failure) come back as
    ``ok=False`` with an ``error`` string instead of raising.
    """
    target = _resolve_inside(sandbox_dir, path)
    if target is None:
        return {"ok": False, "error": "path_outside_sandbox"}
    if not target.exists():
        return {"ok": False, "error": "file_not_found"}
    if not target.is_file():
        return {"ok": False, "error": "not_a_file"}
    try:
        # Read up to max_bytes+1 so we can distinguish exact-fit from truncated.
        cap = max(0, int(max_bytes))
        with target.open("rb") as fh:
            raw = fh.read(cap + 1)
        truncated = len(raw) > cap
        if truncated:
            raw = raw[:cap]
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("utf-8", errors="replace")
        return {
            "ok": True,
            "content": content,
            "bytes_read": len(raw),
            "truncated": truncated,
        }
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}


async def fs_list(
    path: str = ".",
    *,
    sandbox_dir: Any,
) -> dict[str, Any]:
    """List the immediate children of a directory inside the sandbox.

    Returns ``{ok, entries: [{name, is_dir, size}]}``. Hidden files are
    included; the LLM gets the same view as a plain ``ls -la``.
    """
    target = _resolve_inside(sandbox_dir, path or ".")
    if target is None:
        return {"ok": False, "error": "path_outside_sandbox"}
    if not target.exists():
        return {"ok": False, "error": "dir_not_found"}
    if not target.is_dir():
        return {"ok": False, "error": "not_a_directory"}
    try:
        entries: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            is_dir = child.is_dir()
            try:
                size = child.stat().st_size if not is_dir else 0
            except OSError:
                size = -1
            entries.append({"name": child.name, "is_dir": is_dir, "size": size})
        return {"ok": True, "entries": entries}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}


__all__ = ["fs_read", "fs_list"]
