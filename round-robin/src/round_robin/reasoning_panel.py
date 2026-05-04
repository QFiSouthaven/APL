"""Re-exports the canonical ReasoningPanel from sibling prompt-enhancer.

Prompt-enhancer is the single source of truth for the umbrella's LLM
abstractions; this module path-injects its src/ dir at import time and
re-exports the public surface so round-robin code can::

    from round_robin.reasoning_panel import (
        LLMSlot, ReasoningPanel, PanelResult, SlotResponse,
    )

without having to know how the import is bridged.

Long-term (v2.x) the LLM provider abstraction will be extracted into
a standalone ``APL/lab/apl-llm/`` package. For now path-injection is
acceptable — it's the same pattern ``development/llm_client.py`` uses
for ``LMStudioProvider``.

If the sibling repo is unavailable at import time the symbols are bound
to ``None`` (with a captured ``_IMPORT_ERROR``) so module loading itself
never fails — callers that actually try to *use* the panel will get a
clear ``RuntimeError`` from :func:`_require`. This mirrors
``development/llm_client.py``'s degradation pattern.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("round_robin.reasoning_panel")


# This file is at: APL/round-robin/src/round_robin/reasoning_panel.py
#                  parents[0] = round_robin/
#                  parents[1] = src/
#                  parents[2] = round-robin/   (the project root)
#                  parents[3] = APL/
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PE_SRC = _REPO_ROOT / "prompt-enhancer" / "src"
if _PE_SRC.exists() and str(_PE_SRC) not in sys.path:
    sys.path.insert(0, str(_PE_SRC))


_IMPORT_ERROR: Exception | None = None
try:
    from enhancer.llm.reasoning_panel import (  # type: ignore[import-not-found]  # noqa: E402
        DEFAULT_AGGREGATOR,
        DEFAULT_MODE,
        VALID_AGGREGATORS,
        VALID_MODES,
        LLMSlot,
        PanelResult,
        ReasoningPanel,
        SlotResponse,
    )
except Exception as exc:  # pragma: no cover — exercised only when sibling missing
    _IMPORT_ERROR = exc
    DEFAULT_AGGREGATOR = "primary-wins"  # type: ignore[assignment]
    DEFAULT_MODE = "parallel"  # type: ignore[assignment]
    VALID_AGGREGATORS = frozenset()  # type: ignore[assignment]
    VALID_MODES = frozenset()  # type: ignore[assignment]
    LLMSlot = None  # type: ignore[assignment,misc]
    PanelResult = None  # type: ignore[assignment,misc]
    ReasoningPanel = None  # type: ignore[assignment,misc]
    SlotResponse = None  # type: ignore[assignment,misc]
    logger.warning(
        "Could not import ReasoningPanel from sibling prompt-enhancer at %s: %s. "
        "round-robin will only work with reasoning_panel=None until the sibling "
        "is restored.",
        _PE_SRC,
        exc,
    )


def _require(name: str, obj: Any) -> Any:
    """Raise a descriptive RuntimeError if a panel symbol is unavailable."""
    if obj is None:
        raise RuntimeError(
            f"round_robin.reasoning_panel.{name} is unavailable because the "
            f"sibling prompt-enhancer package could not be imported. "
            f"Original error: {_IMPORT_ERROR!r}"
        )
    return obj


__all__ = [
    "DEFAULT_AGGREGATOR", "DEFAULT_MODE", "VALID_AGGREGATORS", "VALID_MODES",
    "LLMSlot", "PanelResult", "ReasoningPanel", "SlotResponse",
    "_require", "_IMPORT_ERROR",
]
