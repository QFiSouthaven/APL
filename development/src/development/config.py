"""Settings — environment-driven config for the development service.

All values can be overridden via env vars so the umbrella's launcher
can point this product at a non-default LM Studio host without code
changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir


def _default_data_dir() -> Path:
    """Where the message board's SQLite file lives.

    ``%APPDATA%/swarm/development/`` on Windows, the XDG analogue
    elsewhere. Same parent directory as ``services.toml`` so all four
    APL products share one config root.
    """
    return Path(user_data_dir("swarm", appauthor=False)) / "development"


@dataclass
class Settings:
    """Runtime config. Read once at startup; can be overridden per-test."""

    # LM Studio (OpenAI-compatible) endpoint.
    provider_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "DEVELOPMENT_PROVIDER_BASE_URL",
            "http://127.0.0.1:1234/v1",
        )
    )
    # Model id to use when the BuildRequest doesn't pin one.
    default_model: str = field(
        default_factory=lambda: os.environ.get(
            "DEVELOPMENT_DEFAULT_MODEL",
            "openai/gpt-oss-120b",
        )
    )
    # HTTP server.
    host: str = field(
        default_factory=lambda: os.environ.get("DEVELOPMENT_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: int(os.environ.get("DEVELOPMENT_PORT", "8767"))
    )
    # Persistent state.
    data_dir: Path = field(default_factory=_default_data_dir)

    @property
    def message_board_path(self) -> Path:
        """SQLite file backing the MessageBoard event log."""
        return self.data_dir / "messageboard.sqlite3"

    def ensure_dirs(self) -> None:
        """Create state directories if they don't exist yet."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


# Module-level singleton; tests build their own.
SETTINGS = Settings()
