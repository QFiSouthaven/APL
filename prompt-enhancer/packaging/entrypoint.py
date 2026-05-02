"""PyInstaller entry point for the windowed launcher.

Boots straight into the NiceGUI Desktop Studio. CLI usage continues to
work via ``pipx install prompt-enhancer`` — the windowed exe is for the
non-Python audience.
"""

from __future__ import annotations

import sys

from enhancer.ui.app import run as run_ui


def main() -> int:
    try:
        run_ui()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        # Any startup failure shows in the system tray notification area
        # and prints to a logfile next to the exe.
        from pathlib import Path

        Path("startup_error.log").write_text(repr(exc), encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
