"""Stages — the layered build pipeline.

Each stage is a small async unit that mutates a shared ``ctx`` dict.
The orchestrator runs them in order and publishes ``STAGE_STARTED`` /
``STAGE_DONE`` / ``STAGE_FAILED`` events to the message board around
each call.

v0.1 implements Architect end-to-end. Coder, Reviewer, Tester, and
Packager are stubs that raise a clear NotImplementedError pointing at
the v2.x roadmap.
"""

from __future__ import annotations

from .architect import ArchitectStage
from .base import Stage
from .coder import CoderStage
from .packager import PackagerStage
from .reviewer import ReviewerStage
from .tester import TesterStage

__all__ = [
    "Stage",
    "ArchitectStage",
    "CoderStage",
    "ReviewerStage",
    "TesterStage",
    "PackagerStage",
]
