"""Sandboxed file-system workspace. All writes are path-checked.

Per-session folder under data/charlie_workspace/session-<ts>/.
Rejects: '..', absolute paths, drive letters, symlink escapes, hidden names,
excessive depth, files over 2 MB. Refuses to delete files Charlie didn't write.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..config import SANDBOX_DIR

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^session-[0-9T_\-]+$")
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_PATH_PARTS = 12
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".bin",
}


class SandboxError(Exception):
    """Raised when a requested path would escape the workspace sandbox."""


class CharlieWorkspace:
    def __init__(self, session_id: str | None = None, base_dir: Path | None = None) -> None:
        if session_id and not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        self._base_dir = (base_dir or SANDBOX_DIR).resolve()
        self.session_id = session_id or self._new_session_id()
        self.root: Path = (self._base_dir / self.session_id).resolve()
        self._written_files: set[Path] = set()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _new_session_id() -> str:
        return "session-" + datetime.now().strftime("%Y%m%dT%H%M%S")

    def _safe_path(self, rel: str) -> Path:
        if not rel or not isinstance(rel, str):
            raise SandboxError("empty path")
        original = rel
        rel = rel.strip().replace("\\", "/")
        if not rel:
            raise SandboxError("empty path after normalization")
        if rel.startswith("/") or rel.startswith("//"):
            raise SandboxError(f"absolute path not allowed: {original!r}")
        if re.match(r"^[A-Za-z]:", rel):
            raise SandboxError(f"absolute path not allowed: {original!r}")

        parts = [p for p in rel.split("/") if p and p != "."]
        if not parts:
            raise SandboxError("path resolves to root")
        if len(parts) > _MAX_PATH_PARTS:
            raise SandboxError(f"path too deep ({len(parts)} > {_MAX_PATH_PARTS})")
        for p in parts:
            if p == "..":
                raise SandboxError(f"traversal not allowed: {rel!r}")
            if p.startswith("."):
                raise SandboxError(f"hidden path not allowed: {p!r}")
            if re.search(r'[<>:"|?*\x00-\x1f]', p):
                raise SandboxError(f"invalid character in path: {p!r}")

        candidate = (self.root / "/".join(parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SandboxError(f"path escapes sandbox: {rel!r}") from exc
        return candidate

    def write(self, rel_path: str, content: str) -> Path:
        data = (content or "").encode("utf-8", errors="replace")
        if len(data) > _MAX_FILE_BYTES:
            raise SandboxError(
                f"file too large ({len(data)} bytes > {_MAX_FILE_BYTES})"
            )
        target = self._safe_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            raise SandboxError(f"target is a directory: {rel_path}")
        target.write_bytes(data)
        self._written_files.add(target)
        logger.info("Charlie wrote %s (%dB)", target.relative_to(self.root), len(data))
        return target

    def mkdir(self, rel_path: str) -> Path:
        target = self._safe_path(rel_path)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def delete(self, rel_path: str) -> bool:
        target = self._safe_path(rel_path)
        if not target.exists():
            return False
        if target.is_dir():
            owned = all(
                p in self._written_files
                for p in target.rglob("*") if p.is_file()
            )
            if not owned:
                raise SandboxError(
                    f"refusing to delete dir with non-Charlie files: {rel_path}"
                )
            shutil.rmtree(target)
            self._written_files = {
                p for p in self._written_files
                if not str(p).startswith(str(target))
            }
            return True
        if target not in self._written_files:
            raise SandboxError(
                f"refusing to delete file Charlie didn't write: {rel_path}"
            )
        target.unlink()
        self._written_files.discard(target)
        return True

    def read(self, rel_path: str, max_bytes: int = _MAX_FILE_BYTES) -> dict:
        target = self._safe_path(rel_path)
        if not target.is_file():
            raise SandboxError(f"not a file: {rel_path}")
        size = target.stat().st_size
        ext = target.suffix.lower()
        if ext in _BINARY_EXTS:
            return {"path": rel_path, "size": size, "binary": True, "content": None,
                    "note": f"binary file ({ext})"}
        if size > max_bytes:
            return {"path": rel_path, "size": size, "binary": False, "content": None,
                    "note": f"file too large to display ({size} B)"}
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"path": rel_path, "size": size, "binary": True, "content": None,
                    "note": "not valid UTF-8"}
        return {"path": rel_path, "size": size, "binary": False, "content": text, "note": ""}

    def tree(self) -> dict:
        return self._tree_at(self.root, "")

    def _tree_at(self, dir_path: Path, rel: str) -> dict:
        node: dict = {
            "name": dir_path.name or self.session_id,
            "path": rel,
            "type": "dir",
            "children": [],
        }
        try:
            entries = sorted(
                dir_path.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except OSError:
            return node
        for entry in entries:
            child_rel = f"{rel}/{entry.name}".lstrip("/") if rel else entry.name
            if entry.is_dir():
                node["children"].append(self._tree_at(entry, child_rel))
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                node["children"].append({
                    "name": entry.name,
                    "path": child_rel,
                    "type": "file",
                    "size": size,
                    "ext": entry.suffix.lower().lstrip("."),
                })
        return node

    def clear(self) -> None:
        for entry in self.root.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass
        self._written_files.clear()

    def stats(self) -> dict:
        n_files = sum(1 for _ in self.root.rglob("*") if _.is_file())
        total = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        return {
            "session_id": self.session_id,
            "root": str(self.root),
            "files": n_files,
            "bytes": total,
            "written_this_session": len(self._written_files),
        }


_current: CharlieWorkspace | None = None


def new_session() -> CharlieWorkspace:
    global _current
    _current = CharlieWorkspace()
    return _current


def get_current() -> CharlieWorkspace | None:
    return _current
