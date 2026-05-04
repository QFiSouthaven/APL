"""User-level UI preferences persisted to data/config.json.

Distinct from `config.py` which holds environment-level paths and timeouts.
This file is the user-facing preferences store: last theme, last model picks,
which intel toggles they want on, whether pause-after-each-turn is the default.

Read on app load (frontend calls GET /api/config), written via debounced
PATCH /api/config from the UI as the user changes inputs.
"""
from __future__ import annotations

from typing import Any

from .config import CONFIG_FILE
from .storage import SafeStorage

DEFAULTS: dict[str, Any] = {
    "theme": "",
    "alpha_name": "Alpha",
    "alpha_model": "",
    "alpha_persona": "",
    "bravo_name": "Bravo",
    "bravo_model": "",
    "bravo_persona": "",
    "loop_limit": 3,
    "pause_after_each_turn": False,
    "auto_retry": 0,
    "charlie_enabled": False,
    "charlie_model": "",
    "intel_collab_directive": True,
    "intel_anti_rambling": True,
    "intel_anti_yes_man": True,
    "intel_agreement_threshold": 2,
}

# Keys we accept on PATCH. Anything else is silently dropped to keep the file
# tidy and to prevent the frontend from stuffing arbitrary state.
ALLOWED_KEYS: frozenset[str] = frozenset(DEFAULTS.keys())


def load() -> dict[str, Any]:
    """Return saved prefs merged on top of DEFAULTS (so missing keys are filled)."""
    saved = SafeStorage.load_json(CONFIG_FILE, {}) or {}
    if not isinstance(saved, dict):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in ALLOWED_KEYS}}


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` into the saved prefs and write atomically. Returns the merged result."""
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")
    current = load()
    for k, v in updates.items():
        if k in ALLOWED_KEYS:
            current[k] = v
    SafeStorage.save_json(CONFIG_FILE, current)
    return current


def reset() -> dict[str, Any]:
    """Wipe to defaults. Returns the defaults dict."""
    SafeStorage.save_json(CONFIG_FILE, dict(DEFAULTS))
    return dict(DEFAULTS)
