"""PresetStore (CRUD + rename + duplicate + import/export) + SessionStore (run history)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MAX_PRESETS, MAX_SESSIONS, PRESETS_FILE, SESSIONS_DIR
from .storage import SafeStorage

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


class PresetStore:
    def __init__(self, path: Path = PRESETS_FILE) -> None:
        self.path = Path(path)

    def list(self) -> list[dict]:
        data = SafeStorage.load_json(self.path, {"presets": []})
        presets = list(data.get("presets") or [])
        presets.sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=True)
        return presets

    def get(self, preset_id: str) -> dict | None:
        for p in self.list():
            if p.get("id") == preset_id:
                return p
        return None

    def create(self, name: str, config: dict) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("Preset name is required.")
        presets = self.list()
        preset = {
            "id": _gen_id("preset"),
            "name": name[:80],
            "config": config,
            "created_at": _now(),
            "updated_at": _now(),
        }
        presets.append(preset)
        self._write(presets)
        return preset

    def update(self, preset_id: str, *, name: str | None = None, config: dict | None = None) -> dict:
        presets = self.list()
        for p in presets:
            if p.get("id") == preset_id:
                if name is not None:
                    p["name"] = name.strip()[:80] or p["name"]
                if config is not None:
                    p["config"] = config
                p["updated_at"] = _now()
                self._write(presets)
                return p
        raise KeyError(preset_id)

    def duplicate(self, preset_id: str) -> dict:
        original = self.get(preset_id)
        if not original:
            raise KeyError(preset_id)
        return self.create(f"{original['name']} (copy)", original.get("config") or {})

    def delete(self, preset_id: str) -> bool:
        presets = self.list()
        kept = [p for p in presets if p.get("id") != preset_id]
        if len(kept) == len(presets):
            return False
        self._write(kept)
        return True

    def import_one(self, preset: dict) -> dict:
        name = (preset.get("name") or "imported").strip() or "imported"
        config = preset.get("config") or {}
        return self.create(name, config)

    def _write(self, presets: list[dict]) -> None:
        SafeStorage.save_json(self.path, {"presets": presets[-MAX_PRESETS:]})


class SessionStore:
    def __init__(self, dir_path: Path = SESSIONS_DIR) -> None:
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, run_state: dict) -> Path:
        run_id = run_state.get("run_id") or _gen_id("run")
        record = {
            "id": run_id,
            "ended_at": _now(),
            **run_state,
        }
        path = self.dir / f"{run_id}.json"
        SafeStorage.save_json(path, record)
        self._prune()
        return path

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix == ".json" and not f.name.endswith((".tmp", ".bak")):
                data = SafeStorage.load_json(f, None)
                if isinstance(data, dict):
                    out.append(_summary(data))
        return out

    def get(self, run_id: str) -> dict | None:
        f = self.dir / f"{run_id}.json"
        if not f.exists():
            return None
        return SafeStorage.load_json(f, None)

    def delete(self, run_id: str) -> bool:
        f = self.dir / f"{run_id}.json"
        if not f.exists():
            return False
        try:
            f.unlink()
            for sib in (f.with_suffix(".json.tmp"), f.with_suffix(".json.bak")):
                if sib.exists():
                    sib.unlink()
        except OSError as exc:
            logger.warning("Could not delete %s: %s", f, exc)
            return False
        return True

    def search(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        if not q:
            return self.list()
        out = []
        for summary in self.list():
            full = self.get(summary["id"]) or {}
            theme = (full.get("config", {}).get("theme") or "").lower()
            if q in theme or q in summary["id"].lower():
                out.append(summary)
                continue
            transcript = full.get("transcript") or []
            if any(q in (e.get("content") or "").lower() for e in transcript):
                out.append(summary)
        return out

    def _prune(self) -> None:
        files = sorted(self.dir.glob("run-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(files) <= MAX_SESSIONS:
            return
        for f in files[MAX_SESSIONS:]:
            try:
                f.unlink()
            except OSError:
                pass


def _summary(data: dict) -> dict[str, Any]:
    cfg = data.get("config") or {}
    return {
        "id": data.get("id") or data.get("run_id"),
        "status": data.get("status"),
        "theme": cfg.get("theme") or "",
        "turns": data.get("current_turn") or 0,
        "ended_at": data.get("ended_at") or data.get("updated_at"),
        "agents": [a.get("name") for a in cfg.get("agents") or []],
    }
