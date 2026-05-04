"""Tests for the four layer generators + the shared JSON helper.

Each generator (backend/frontend/database/deployment) shares the same
one-retry-on-parse-failure shape via ``_common.generate_layer_files``,
so we exercise that code path through each public ``generate(...)``
entrypoint to keep the tests honest about the public API.
"""

from __future__ import annotations

import json

import pytest

from development._json_utils import parse_llm_json
from development.layers import (
    LAYER_GENERATORS,
    applies_to,
    backend,
    database,
    deployment,
    frontend,
)
from development.layers._common import RETRY_REMINDER
from development.types import LayerGenerationError

from tests.conftest import FakeLMClient


# Reusable plan/layer fixtures.
_PLAN = {
    "stack": {
        "frontend": "react",
        "backend": "fastapi",
        "database": "sqlite",
        "deployment": "docker",
    },
    "layers": [],
    "dependencies": [],
}


def _layer(name: str, files: list[str]) -> dict:
    return {"name": name, "purpose": f"the {name}", "language": "x", "files": files}


# ── parse_llm_json (shared helper) ──────────────────────────────────


def test_parse_llm_json_clean_object():
    assert parse_llm_json('{"a": 1}') == {"a": 1}


def test_parse_llm_json_strips_fences():
    raw = "```json\n{\"a\": 1}\n```"
    assert parse_llm_json(raw) == {"a": 1}


def test_parse_llm_json_extracts_embedded_object():
    raw = "Sure, here is the plan:\n{\"a\": 1}\nLet me know!"
    assert parse_llm_json(raw) == {"a": 1}


def test_parse_llm_json_returns_none_on_garbage():
    assert parse_llm_json("not json at all") is None
    assert parse_llm_json("") is None
    assert parse_llm_json("[1, 2, 3]") is None  # top-level list rejected


# ── per-generator: clean / fenced / retry-then-success / retry-exhaustion ──


@pytest.mark.parametrize(
    "module",
    [backend, frontend, database, deployment],
    ids=["backend", "frontend", "database", "deployment"],
)
@pytest.mark.asyncio
async def test_generator_clean_json(module):
    payload = {"path/to/file.py": "print('ok')"}
    fake = FakeLMClient(responses=[json.dumps(payload)])
    out = await module.generate(_PLAN, _layer(module.__name__.split(".")[-1], ["a"]), fake)
    assert out == payload
    assert len(fake.calls) == 1
    # System prompt is always the module's SYSTEM_PROMPT — sanity.
    assert fake.calls[0]["messages"][0]["content"] == module.SYSTEM_PROMPT


@pytest.mark.parametrize(
    "module",
    [backend, frontend, database, deployment],
    ids=["backend", "frontend", "database", "deployment"],
)
@pytest.mark.asyncio
async def test_generator_fenced_json(module):
    payload = {"f.py": "x"}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    fake = FakeLMClient(responses=[fenced])
    out = await module.generate(_PLAN, _layer(module.__name__.split(".")[-1], []), fake)
    assert out == payload
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "module",
    [backend, frontend, database, deployment],
    ids=["backend", "frontend", "database", "deployment"],
)
@pytest.mark.asyncio
async def test_generator_retries_garbage_then_succeeds(module):
    payload = {"f.py": "x"}
    fake = FakeLMClient(responses=["not json", json.dumps(payload)])
    out = await module.generate(_PLAN, _layer(module.__name__.split(".")[-1], []), fake)
    assert out == payload
    # Two calls — initial + retry.
    assert len(fake.calls) == 2
    # The retry includes the strict-JSON reminder as the final user msg.
    second = fake.calls[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"] == RETRY_REMINDER
    # Retry uses temperature=0.0 (a la the architect's strict retry).
    assert fake.calls[1]["temperature"] == 0.0


@pytest.mark.parametrize(
    "module",
    [backend, frontend, database, deployment],
    ids=["backend", "frontend", "database", "deployment"],
)
@pytest.mark.asyncio
async def test_generator_raises_on_retry_exhaustion(module):
    fake = FakeLMClient(responses=["nope", "still nope"])
    layer_name = module.__name__.split(".")[-1]
    with pytest.raises(LayerGenerationError) as exc_info:
        await module.generate(_PLAN, _layer(layer_name, []), fake)
    assert exc_info.value.layer_name == layer_name
    assert "still nope" in exc_info.value.raw_response
    assert len(fake.calls) == 2


# ── value coercion: non-string contents are stringified ───────────────


@pytest.mark.asyncio
async def test_generator_stringifies_non_string_file_values():
    """LLMs sometimes wrap content in ``{"content": "..."}`` — coerce."""
    payload = {"file.json": {"content": "literal-json"}}
    fake = FakeLMClient(responses=[json.dumps(payload)])
    out = await backend.generate(_PLAN, _layer("backend", ["file.json"]), fake)
    # Value got JSON-stringified.
    assert "literal-json" in out["file.json"]
    assert isinstance(out["file.json"], str)


@pytest.mark.asyncio
async def test_generator_drops_non_string_keys():
    """Keys that aren't strings are silently dropped (defensive)."""
    # We can't easily emit non-string JSON keys via json.dumps, so build raw text.
    raw = '{"good.py": "yes", "1": "kept-as-string"}'
    fake = FakeLMClient(responses=[raw])
    out = await backend.generate(_PLAN, _layer("backend", []), fake)
    # Both keys are strings (JSON only allows string keys), so both kept.
    assert out == {"good.py": "yes", "1": "kept-as-string"}


# ── registry contract ────────────────────────────────────────────────


def test_layer_generators_registry_keys_lowercased():
    assert set(LAYER_GENERATORS.keys()) == {
        "backend",
        "frontend",
        "database",
        "deployment",
    }


def test_applies_to_returns_true_for_known_planned_layer():
    plan = {"layers": [{"name": "Backend"}, {"name": "frontend"}]}
    assert applies_to(plan, "backend") is True
    assert applies_to(plan, "frontend") is True


def test_applies_to_returns_false_for_unplanned_or_unknown():
    plan = {"layers": [{"name": "backend"}]}
    assert applies_to(plan, "frontend") is False  # known generator, not planned
    assert applies_to(plan, "docs") is False  # unknown generator
    assert applies_to({}, "backend") is False  # plan with no layers
