"""Runtime configuration — env-overridable, no module-level singleton.

Important deviation from the source monolith: the previous
``model_config.py`` ran ``load()`` at import time and cached the active
model name in a module-global dict. That singleton-at-import pattern
caused stale-model bugs (the dropdown said one thing while the pipeline
called another). Here we read on demand instead.

Settings file lives at:
    Windows: %APPDATA%\\prompt-enhancer\\settings.toml
    Linux/macOS: ~/.config/prompt-enhancer/settings.toml

Precedence (lowest → highest): dataclass defaults < TOML file < env vars.
The TOML file is optional — if absent or malformed, ``load()`` falls
back to defaults (with env-var overrides) and never raises.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

APP_NAME = "prompt-enhancer"

logger = logging.getLogger("enhancer.config")


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
    scorer_model: str = ""   # same as default_model when empty
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


# Map dataclass-field-name → ENHANCER_<NAME> env-var suffix.
_ENV_SUFFIX = {
    "provider": "PROVIDER",
    "lms_base_url": "LMS_BASE_URL",
    "lms_management_url": "LMS_MANAGEMENT_URL",
    "default_model": "DEFAULT_MODEL",
    "scorer_model": "SCORER_MODEL",
    "request_timeout": "REQUEST_TIMEOUT",
    "idle_timeout": "IDLE_TIMEOUT",
    "temperature": "TEMPERATURE",
    "max_tokens_scale": "MAX_TOKENS_SCALE",
    "disambiguate_threshold": "DISAMBIGUATE_THRESHOLD",
    "ui_port": "UI_PORT",
    "ui_host": "UI_HOST",
    "methodology_agent_enabled": "METHODOLOGY_AGENT_ENABLED",
}


def _coerce(field_type: type, value: Any, default: Any) -> Any:
    """Coerce ``value`` into ``field_type``; return ``default`` on failure."""
    try:
        if field_type is bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if field_type is int:
            return int(value)
        if field_type is float:
            return float(value)
        if field_type is str:
            return str(value)
    except (TypeError, ValueError):
        return default
    return default


def _read_toml_file(path: Path) -> dict[str, Any]:
    """Parse the settings TOML file; return {} if missing or malformed."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Malformed settings TOML at %s: %s — using defaults", path, exc)
        # Try the .bak as a last-ditch recovery.
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            try:
                with bak.open("rb") as f:
                    data = tomllib.load(f)
                if isinstance(data, dict):
                    logger.info("Recovered settings from %s", bak)
                    return data
            except (OSError, tomllib.TOMLDecodeError):
                pass
        return {}


def load() -> Settings:
    """Read settings with precedence: defaults < TOML file < env vars.

    The TOML file is optional. Env vars use the form
    ``ENHANCER_<UPPER_FIELD_NAME>``. Failures at any layer fall back to
    the layer below — this function never raises.
    """
    defaults = Settings()
    toml_data = _read_toml_file(settings_path())

    values: dict[str, Any] = {}
    for f in fields(Settings):
        default = getattr(defaults, f.name)
        # ``from __future__ import annotations`` means f.type is a string
        # — derive the real type from the default value instead. (All
        # Settings fields use bool/int/float/str defaults, never None.)
        ftype = type(default)
        # Layer 1: TOML
        if f.name in toml_data:
            values[f.name] = _coerce(ftype, toml_data[f.name], default)
        else:
            values[f.name] = default
        # Layer 2: env override
        env_name = f"ENHANCER_{_ENV_SUFFIX[f.name]}"
        if env_name in os.environ:
            values[f.name] = _coerce(ftype, os.environ[env_name], values[f.name])

    return Settings(**values)


def _atomic_write_toml(path: Path, payload: dict[str, Any]) -> None:
    """Atomic-rename write with .bak retention of the previous file."""
    import tomli_w  # runtime dep; lazy-imported so module load doesn't fail when settings are never written

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")
    serialized = tomli_w.dumps(payload)
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            try:
                bak.write_bytes(path.read_bytes())
            except OSError as exc:
                logger.warning("Backup write failed for %s: %s", path, exc)
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def save_settings(settings: Settings) -> Path:
    """Write ``settings`` to the TOML file (atomic, .bak-backed).

    Returns the absolute path written.
    """
    payload: dict[str, Any] = {f.name: getattr(settings, f.name) for f in fields(Settings)}
    target = settings_path()
    _atomic_write_toml(target, payload)
    return target.resolve()
