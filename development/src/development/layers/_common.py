"""Internal helpers shared by every layer generator.

Each generator (``backend.py``, ``frontend.py``, …) calls
:func:`generate_layer_files` with its own system prompt and the
plan/layer/llm triple. This module owns the LLM-call-with-one-retry
pattern so the per-layer files stay focused on prompt content.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .._json_utils import parse_llm_json
from ..llm_client import LLMClient
from ..types import LayerGenerationError

logger = logging.getLogger("development.layers")


# Strict-retry reminder appended after a parse failure. Mirrors the
# Architect's RETRY_REMINDER but mentions the file-map shape.
RETRY_REMINDER = (
    "Your previous response could not be parsed as JSON. Output ONLY a "
    "valid JSON object whose keys are file paths and whose values are "
    "the file contents as strings. No markdown fences, no commentary."
)


def build_user_prompt(
    plan: dict[str, Any],
    layer: dict[str, Any],
) -> str:
    """Render the user-message body for a layer generator.

    The plan and the specific layer dict are both rendered as JSON so
    the LLM sees the full context (stack picks, dependencies) plus the
    narrowed file list it should generate.
    """
    return (
        f"Plan:\n{json.dumps(plan, indent=2)}\n\n"
        f"Layer:\n{json.dumps(layer, indent=2)}\n\n"
        "Generate the file contents for this layer's `files` list. "
        "Output a JSON object mapping each file path to its contents."
    )


async def generate_layer_files(
    llm: LLMClient,
    *,
    system_prompt: str,
    plan: dict[str, Any],
    layer: dict[str, Any],
) -> dict[str, str]:
    """Drive the one-retry LLM-call loop for a single layer.

    Returns the parsed ``{path: content}`` dict on success.
    Raises :class:`LayerGenerationError` if both attempts fail to parse.
    """
    layer_name = str(layer.get("name", "unknown"))
    user_prompt = build_user_prompt(plan, layer)

    # First attempt.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw = await llm.chat(messages, temperature=0.2, max_tokens=4096)
    parsed = parse_llm_json(raw)

    # One retry on parse failure.
    if parsed is None:
        logger.warning(
            "Layer %s: first response failed to parse; retrying once. Raw=%r",
            layer_name,
            raw[:200],
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": raw},
            {"role": "user", "content": RETRY_REMINDER},
        ]
        raw = await llm.chat(messages, temperature=0.0, max_tokens=4096)
        parsed = parse_llm_json(raw)

    if parsed is None:
        raise LayerGenerationError(layer_name, raw_response=raw)

    # Coerce: keys must be str, values must be str. Drop entries that
    # don't fit the shape rather than crash — the LLM occasionally wraps
    # a value in a dict (``{"content": "..."}``); we stringify it.
    result: dict[str, str] = {}
    for path, content in parsed.items():
        if not isinstance(path, str):
            continue
        if isinstance(content, str):
            result[path] = content
        else:
            result[path] = json.dumps(content, indent=2)
    return result
