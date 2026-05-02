"""Atomic JSON read/write helper — ported from ``src/core/system.py``.

Used for the templates export / import path and any settings file we
write outside SQLite. Sessions go through SQLite so this is no longer in
the pipeline's hot path, but the helper is retained for utility.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("enhancer.persistence.safestorage")


class SafeStorage:
    """Atomic write + automatic backup on corruption.

    ``save_json`` writes ``<path>.tmp`` then ``os.replace``s into
    ``<path>``, after first copying the previous file to ``<path>.bak``.
    ``load_json`` falls back to ``<path>.bak`` if the primary file is
    truncated / corrupted.
    """

    @staticmethod
    def save_json(path: str | Path, data: Any) -> None:
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                try:
                    bak.write_bytes(path.read_bytes())
                except OSError as exc:
                    logger.warning("Backup write failed for %s: %s", path, exc)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("SafeStorage.save_json failed for %s: %s", path, exc)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def load_json(path: str | Path, default: Any) -> Any:
        path = Path(path)
        if not path.exists():
            return default
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("CORRUPTION DETECTED in %s: %s — falling back to .bak", path, exc)
            if bak.exists():
                try:
                    with bak.open("r", encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError) as exc2:
                    logger.warning("Backup also unreadable: %s", exc2)
        return default
