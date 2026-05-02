"""JSONL dual-writer — kept ONE release for ``devflow.py`` compat.

The format here matches ``swarm-agent-dev/src/webui/mods/agent_pipeline.py
::_log_pipeline_run`` byte-for-byte. Field order, key names, and the
"only include if truthy" rule for optional fields are all preserved.

After the migration window we:

1. Document the deprecation in ``docs/MIGRATION.md``.
2. Drop this writer.
3. ``devflow.py`` (in the monolith) gets a small adapter to read SQLite
   directly via the standalone's exported runs API — or stays on JSONL
   if the monolith is being sunset.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runs import RunRecord

logger = logging.getLogger("enhancer.persistence.jsonl_compat")


def append(record: RunRecord, path: Path) -> None:
    """Append one JSON line for the given run.

    Field order and the optional-field rule MUST match
    ``_log_pipeline_run`` exactly:

      1. Always present: ts, prompt, technique, intent_preview,
         weakness_preview, enhanced_preview.
      2. Conditionally present (only when truthy): task_type, scores,
         pass_times_ms, scorer_model, model, pass1_output, pass2_output,
         pass4_output, persona.
    """
    entry: dict[str, object] = {
        "ts": record.ts,
        "prompt": record.prompt,
        "technique": record.technique,
        "intent_preview": record.pass1_output[:500],
        "weakness_preview": record.pass2_output[:500],
        "enhanced_preview": record.enhanced_prompt,
    }
    if record.task_type:
        entry["task_type"] = record.task_type
    if record.scores:
        entry["scores"] = record.scores
    if record.pass_times_ms:
        entry["pass_times_ms"] = record.pass_times_ms
    if record.scorer_model:
        entry["scorer_model"] = record.scorer_model
    if record.model:
        entry["model"] = record.model
    if record.pass1_output:
        entry["pass1_output"] = record.pass1_output
    if record.pass2_output:
        entry["pass2_output"] = record.pass2_output
    if record.pass4_output:
        entry["pass4_output"] = record.pass4_output
    if record.persona:
        entry["persona"] = record.persona

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("JSONL dual-write failed: %s", exc)
