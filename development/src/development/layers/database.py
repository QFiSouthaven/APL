"""Database layer generator.

Produces schema migrations and ORM model files honoring
``plan["stack"]["database"]`` (sqlite, postgres, mysql, …). The Coder
generates schema; the Tester (later) handles migration tests.
"""

from __future__ import annotations

from typing import Any

from ..llm_client import LLMClient
from ._common import generate_layer_files

SYSTEM_PROMPT = (
    "You are a database engineer. Given the plan + this specific layer's "
    "purpose + file list, output a JSON object mapping each requested "
    "file path to its file contents (as a string). Generate schema "
    "migrations and ORM/model files honoring plan.stack.database "
    "(sqlite, postgres, mysql, etc.). Output ONLY valid JSON, no prose, "
    "no markdown fences."
)


async def generate(
    plan: dict[str, Any],
    layer: dict[str, Any],
    llm: LLMClient,
) -> dict[str, str]:
    """Generate database layer files. Returns ``{path: content}``."""
    return await generate_layer_files(
        llm,
        system_prompt=SYSTEM_PROMPT,
        plan=plan,
        layer=layer,
    )
