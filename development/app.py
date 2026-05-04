"""Entry point — ``python app.py`` runs the development server.

Equivalent to ``uvicorn development.server:app --host 127.0.0.1 --port 8767``;
exists so the umbrella's launcher can spawn the product with a single
predictable command.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    # Make ``development`` importable when running from a checkout
    # without an editable install.
    src_dir = Path(__file__).resolve().parent / "src"
    if src_dir.exists() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    import uvicorn

    from development.config import SETTINGS

    uvicorn.run(
        "development.server:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
