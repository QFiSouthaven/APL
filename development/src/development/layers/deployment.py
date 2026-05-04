"""Deployment layer generator.

Produces Dockerfiles, docker-compose configs, and shell scripts that
match ``plan["stack"]["deployment"]`` (docker, docker-compose, bare
shell, kubernetes, etc.).
"""

from __future__ import annotations

from typing import Any

from ..llm_client import LLMClient
from ._common import generate_layer_files

SYSTEM_PROMPT = (
    "You are a DevOps engineer. Given the plan + this specific layer's "
    "purpose + file list, output a JSON object mapping each requested "
    "file path to its file contents (as a string). Generate Dockerfile, "
    "docker-compose, and/or shell scripts honoring plan.stack.deployment. "
    "Output ONLY valid JSON, no prose, no markdown fences."
)


async def generate(
    plan: dict[str, Any],
    layer: dict[str, Any],
    llm: LLMClient,
) -> dict[str, str]:
    """Generate deployment layer files. Returns ``{path: content}``."""
    return await generate_layer_files(
        llm,
        system_prompt=SYSTEM_PROMPT,
        plan=plan,
        layer=layer,
    )
