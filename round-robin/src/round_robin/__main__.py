"""Allow `python -m round_robin` to launch the desktop app."""
from pathlib import Path
import sys

# Add project root so app.py is importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def main() -> None:
    from app import main as launch
    launch()


if __name__ == "__main__":
    main()
