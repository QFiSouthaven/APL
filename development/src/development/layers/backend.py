"""Backend layer generator.

Produces server-side source files (route handlers, app entrypoint,
middleware) for the framework picked in ``plan["stack"]["backend"]``
(FastAPI, Flask, Express, …). The system prompt asks the LLM to
respect the framework choice; the generator does not enforce a
specific runtime — that's intentional so the matrix stays open.
"""

from __future__ import annotations

from typing import Any

from ..llm_client import LLMClient
from ._common import generate_layer_files

SYSTEM_PROMPT = (
    "You are a backend engineer. Given the plan + this specific layer's "
    "purpose + file list, output a JSON object mapping each requested "
    "file path to its file contents (as a string). Honor the framework "
    "in plan.stack.backend (FastAPI, Flask, Express, etc.). Output ONLY "
    "valid JSON, no prose, no markdown fences."
)


async def generate(
    plan: dict[str, Any],
    layer: dict[str, Any],
    llm: LLMClient,
) -> dict[str, str]:
    """Generate backend layer files. Returns ``{path: content}``."""
    return await generate_layer_files(
        llm,
        system_prompt=SYSTEM_PROMPT,
        plan=plan,
        layer=layer,
    )
