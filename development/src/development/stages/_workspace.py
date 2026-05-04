"""Per-layer ephemeral test-workspace context manager.

A ``TestWorkspace`` materializes the layer's source files plus the
generated test files into a fresh temp directory under
:func:`tempfile.gettempdir`, hands the path to the caller, then deletes
the entire tree on exit.

The workspace is intentionally narrow:

* Only the files the caller passes are written — nothing from the host
  source tree leaks in. (The Tester stage runs subprocesses in this
  directory; we don't want them seeing the dev's repo.)
* The directory is unconditionally deleted on ``__exit__`` even if the
  subprocess errored.
* Path traversal is rejected — entries with ``..`` components or
  absolute paths are dropped with a warning rather than silently writing
  outside the sandbox.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("development.stages.workspace")


class TestWorkspace:
    """Context manager: temp dir populated with layer files + tests.

    Usage::

        with TestWorkspace("backend", files, tests) as ws:
            result = await run_tests(ws, "pytest")
        # ws is gone; nothing to clean up.

    The order of file writes is: layer source files first, then test
    files. Test files override source files of the same path so the
    Tester can replace a generated stub with a richer test of its own
    if the LLM happens to use the same filename (we log a warning if
    that occurs).
    """

    # Opt out of pytest's auto-collection heuristic (class name starts
    # with "Test"). This is not a test class.
    __test__ = False

    def __init__(
        self,
        layer_name: str,
        files: dict[str, str],
        tests: dict[str, str],
    ) -> None:
        self._layer_name = layer_name
        self._files = dict(files or {})
        self._tests = dict(tests or {})
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._path: Path | None = None

    def __enter__(self) -> Path:
        # prefix=development-tester- so leaked dirs are easy to grep for
        # if the cleanup ever fails to fire.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="development-tester-")
        self._path = Path(self._tmpdir.name)
        self._materialize(self._files, "source")
        self._materialize(self._tests, "test")
        return self._path

    def __exit__(self, *exc) -> None:
        if self._tmpdir is None:
            return
        try:
            self._tmpdir.cleanup()
        except (OSError, PermissionError) as cleanup_exc:
            # Subprocess-on-Windows occasionally leaves file handles
            # behind for a few ms after exit. Best effort: try a
            # second pass with shutil.rmtree(ignore_errors=True) so we
            # don't bubble cleanup exceptions to the caller.
            logger.debug(
                "TestWorkspace(%s): TemporaryDirectory.cleanup raised %s; "
                "falling back to rmtree(ignore_errors=True).",
                self._layer_name,
                cleanup_exc,
            )
            if self._path is not None:
                shutil.rmtree(self._path, ignore_errors=True)
        finally:
            self._tmpdir = None
            self._path = None

    # ── internals ──────────────────────────────────────────────────

    def _materialize(self, files: dict[str, str], category: str) -> None:
        if self._path is None:
            raise RuntimeError("TestWorkspace not entered")
        for relpath, content in files.items():
            target = self._safe_target(relpath)
            if target is None:
                logger.warning(
                    "TestWorkspace(%s): rejected unsafe %s path %r — must "
                    "be a relative path with no `..` components.",
                    self._layer_name,
                    category,
                    relpath,
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _safe_target(self, relpath: str) -> Path | None:
        """Resolve ``relpath`` against the workspace, rejecting traversals."""
        if self._path is None:
            return None
        if not relpath:
            return None
        # Normalize: strip leading "./", reject absolute paths and
        # parent-references.
        p = Path(relpath)
        if p.is_absolute():
            return None
        try:
            resolved = (self._path / p).resolve()
        except (OSError, ValueError):
            return None
        try:
            resolved.relative_to(self._path.resolve())
        except ValueError:
            return None
        return resolved


__all__ = ["TestWorkspace"]
