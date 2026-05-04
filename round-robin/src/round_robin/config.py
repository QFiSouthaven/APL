"""Paths, defaults, env-var overrides."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
DATA_DIR: Path = Path(os.getenv("ROUND_ROBIN_DATA_DIR", str(PROJECT_ROOT / "data")))
SESSIONS_DIR: Path = DATA_DIR / "sessions"
SANDBOX_DIR: Path = DATA_DIR / "charlie_workspace"
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

CONFIG_FILE: Path = DATA_DIR / "config.json"
STATE_FILE: Path = DATA_DIR / "state.json"
PRESETS_FILE: Path = DATA_DIR / "presets.json"

LMS_BASE_URL: str = os.getenv("LMS_BASE_URL", "http://localhost:1234/v1")
LMS_TIMEOUT_CONNECT: float = float(os.getenv("LMS_TIMEOUT_CONNECT", "5.0"))
LMS_TIMEOUT_READ: float = float(os.getenv("LMS_TIMEOUT_READ", "300.0"))
LMS_TIMEOUT_WRITE: float = float(os.getenv("LMS_TIMEOUT_WRITE", "30.0"))

DEFAULT_LOOP_LIMIT: int = 3
MAX_PRESETS: int = 200
MAX_SESSIONS: int = 500

# Whitespace-token estimate cap for the transcript Charlie summarizes. Local
# Llama-class models default to 4096-8192 token contexts; budget ~25% for the
# system prompt + Charlie's own output, leaving ~6000 for the transcript.
CHARLIE_INPUT_TOKEN_LIMIT: int = int(os.getenv("CHARLIE_INPUT_TOKEN_LIMIT", "6000"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
