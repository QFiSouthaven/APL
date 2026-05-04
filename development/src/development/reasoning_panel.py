"""Re-export :mod:`enhancer.llm.reasoning_panel` for the development service.

Mirrors the path-injection pattern in :mod:`development.llm_client`. We
share ONE canonical ``ReasoningPanel`` implementation across products
by injecting the prompt-enhancer ``src/`` directory at import time
rather than vendoring a copy. v2.x will extract the abstraction into a
standalone ``apl-llm`` package; for now path injection keeps the
umbrella honest about there being a single multi-LLM reasoning surface.

Usage::

    from development.reasoning_panel import LLMSlot, ReasoningPanel
    panel = ReasoningPanel([LLMSlot("primary", provider, "qwen3-coder")])
    result = await panel.consult(messages, mode="parallel",
                                  aggregator="primary-wins")

Import-time tolerance: if the sibling prompt-enhancer ``src/`` is
missing, this module still imports cleanly and exposes the names as
``None``. A subsequent ``ReasoningPanel(...)`` call (or any other
constructor / use) raises ``ImportError`` carrying the original cause.
This mirrors :mod:`development.llm_client`'s ``_IMPORT_ERROR``
contract so import-time failures don't cascade into the orchestrator's
own import graph.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("development.reasoning_panel")


# Find APL/prompt-enhancer/src and prepend to sys.path so we can import
# ``enhancer.llm.reasoning_panel`` without a pip install.
#
# This file is at: APL/development/src/development/reasoning_panel.py
#                  parents[0] = development/
#                  parents[1] = src/
#                  parents[2] = development/   (the project root)
#                  parents[3] = APL/
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PE_SRC = _REPO_ROOT / "prompt-enhancer" / "src"
if _PE_SRC.exists() and str(_PE_SRC) not in sys.path:
    sys.path.insert(0, str(_PE_SRC))


# Late import — exercised in production. On failure we keep the names
# bound to ``None`` and stash the exception so callers get a useful
# message when they actually try to instantiate something.
try:
    from enhancer.llm.reasoning_panel import (  # type: ignore[import-not-found]
        DEFAULT_AGGREGATOR,
        DEFAULT_MODE,
        VALID_AGGREGATORS,
        VALID_MODES,
        LLMSlot,
        PanelResult,
        ReasoningPanel as _ReasoningPanel,
        SlotResponse,
    )

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover — exercised only when sibling missing
    _ReasoningPanel = None  # type: ignore[assignment,misc]
    LLMSlot = None  # type: ignore[assignment,misc]
    PanelResult = None  # type: ignore[assignment,misc]
    SlotResponse = None  # type: ignore[assignment,misc]
    DEFAULT_MODE = None  # type: ignore[assignment]
    DEFAULT_AGGREGATOR = None  # type: ignore[assignment]
    VALID_MODES = None  # type: ignore[assignment]
    VALID_AGGREGATORS = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
    logger.warning(
        "Could not import ReasoningPanel from sibling prompt-enhancer at %s: %s. "
        "ReasoningPanel will only work if the sibling is restored.",
        _PE_SRC,
        exc,
    )


if _ReasoningPanel is not None:
    # Re-export under the canonical name so callers can do
    # ``from development.reasoning_panel import ReasoningPanel``.
    ReasoningPanel = _ReasoningPanel
else:  # pragma: no cover — only on broken installs

    class ReasoningPanel:  # type: ignore[no-redef]
        """Stub raised on use when the sibling import failed.

        We deliberately let the module load so unrelated imports of
        :mod:`development.reasoning_panel` don't crash. The first call
        site that actually tries to construct a panel gets a clear
        ``ImportError`` carrying the original cause.
        """

        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            raise ImportError(
                "ReasoningPanel is unavailable: prompt-enhancer sibling import "
                f"failed. Original error: {_IMPORT_ERROR!r}"
            )


__all__ = [
    "LLMSlot",
    "ReasoningPanel",
    "PanelResult",
    "SlotResponse",
    "DEFAULT_MODE",
    "DEFAULT_AGGREGATOR",
    "VALID_MODES",
    "VALID_AGGREGATORS",
]
