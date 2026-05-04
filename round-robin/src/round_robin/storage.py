"""Atomic JSON persistence with .bak fallback. Ported from swarm-agent-dev."""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SafeStorage:
    """Atomic write via temp + rename; on read corruption, fall back to .bak."""

    @staticmethod
    def save_json(filepath: str | Path, data: Any) -> None:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        backup_path = path.with_suffix(path.suffix + ".bak")

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())

        if path.exists():
            try:
                shutil.copy2(path, backup_path)
            except OSError as exc:
                logger.warning("Backup copy failed for %s: %s", path, exc)

        os.replace(temp_path, path)

    @staticmethod
    def load_json(filepath: str | Path, default: Any) -> Any:
        path = Path(filepath)
        if not path.exists():
            return default

        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Primary read failed for %s: %s", path, exc)
            backup_path = path.with_suffix(path.suffix + ".bak")
            if backup_path.exists():
                try:
                    with open(backup_path, encoding="utf-8") as f:
                        logger.info("Recovered %s from .bak", path)
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as exc2:
                    logger.error("Backup read also failed for %s: %s", backup_path, exc2)
            return default
