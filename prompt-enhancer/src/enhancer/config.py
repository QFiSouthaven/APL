"""Runtime configuration — env-overridable, no module-level singleton.

Important deviation from the source monolith: the previous
``model_config.py`` ran ``load()`` at import time and cached the active
model name in a module-global dict. That singleton-at-import pattern
caused stale-model bugs (the dropdown said one thing while the pipeline
called another). Here we read on demand instead.

Settings file lives at:
    Windows: %APPDATA%\\prompt-enhancer\\settings.toml
    Linux/macOS: ~/.config/prompt-enhancer/settings.toml
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "prompt-enhancer"


# ── paths ──────────────────────────────────────────────────────────────────
def config_dir() -> Path:
    """Platform-appropriate config directory; created on first call."""
    p = Path(user_config_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Platform-appropriate data directory; created on first call."""
    p = Path(user_data_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    """SQLite database path."""
    return data_dir() / "enhancer.db"


def jsonl_log_path() -> Path:
    """JSONL pipeline log (kept for one release for devflow.py compat)."""
    return data_dir() / "agent_pipeline.log"


def settings_path() -> Path:
    return config_dir() / "settings.toml"


# ── settings model ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    # LLM backend
    provider: str = "lmstudio"
    lms_base_url: str = "http://127.0.0.1:1234/v1"
    lms_management_url: str = "http://localhost:1234"
    default_model: str = ""  # empty → first available at runtime
    scorer_model: str = ""   # empty → same as default_model
    request_timeout: float = 600.0
    idle_timeout: float = 120.0

    # Pipeline knobs
    temperature: float = 0.7
    max_tokens_scale: float = 1.0
    disambiguate_threshold: int = 3

    # UI
    ui_port: int = 8765
    ui_host: str = "127.0.0.1"

    # Methodology agent
    methodology_agent_enabled: bool = True


def load() -> Settings:
    """Read settings from env vars; the TOML file is layered in v0.2.

    Env vars take the form ``ENHANCER_<UPPER_FIELD_NAME>``.
    """
    def _get(name: str, default: str) -> str:
        return os.environ.get(f"ENHANCER_{name}", default)

    def _getf(name: str, default: float) -> float:
        try:
            return float(_get(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _geti(name: str, default: int) -> int:
        try:
            return int(_get(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _getb(name: str, default: bool) -> bool:
        raw = _get(name, "1" if default else "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    return Settings(
        provider=_get("PROVIDER", "lmstudio"),
        lms_base_url=_get("LMS_BASE_URL", "http://127.0.0.1:1234/v1"),
        lms_management_url=_get("LMS_MANAGEMENT_URL", "http://localhost:1234"),
        default_model=_get("DEFAULT_MODEL", ""),
        scorer_model=_get("SCORER_MODEL", ""),
        request_timeout=_getf("REQUEST_TIMEOUT", 600.0),
        idle_timeout=_getf("IDLE_TIMEOUT", 120.0),
        temperature=_getf("TEMPERATURE", 0.7),
        max_tokens_scale=_getf("MAX_TOKENS_SCALE", 1.0),
        disambiguate_threshold=_geti("DISAMBIGUATE_THRESHOLD", 3),
        ui_port=_geti("UI_PORT", 8765),
        ui_host=_get("UI_HOST", "127.0.0.1"),
        methodology_agent_enabled=_getb("METHODOLOGY_AGENT_ENABLED", True),
    )
