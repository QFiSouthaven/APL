"""Shared fixtures: FakeLMClient + tmp message-board DB."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from development.llm_client import LLMClient
from development.messageboard import MessageBoard


class FakeLMClient(LLMClient):
    """LLMClient with a scripted ``chat`` method.

    Construct with either:
      * ``responses=[str, ...]``    — pop one response per chat call.
      * ``responder=Callable``      — full control (sees messages + kwargs).

    Records every call in ``self.calls`` for assertions.
    """

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        responder: Callable[..., str] | None = None,
        default_model: str = "fake-model",
    ) -> None:
        # Skip parent __init__ entirely — we don't need a real provider.
        self._provider = None  # type: ignore[assignment]
        self._default_model = default_model
        self._responses = list(responses or [])
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> str:
        call = {
            "messages": messages,
            "model": model or self._default_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        self.calls.append(call)
        if self._responder is not None:
            return self._responder(messages=messages, **{
                "model": call["model"],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": timeout,
            })
        if not self._responses:
            return json.dumps({"stack": {}, "layers": [], "dependencies": []})
        return self._responses.pop(0)


@pytest.fixture
def fake_lm() -> FakeLMClient:
    """Default fake — returns a minimal valid Architect plan."""
    return FakeLMClient(
        responses=[
            json.dumps(
                {
                    "stack": {
                        "frontend": "html",
                        "backend": "python",
                        "database": "sqlite",
                        "deployment": "local",
                    },
                    "layers": [
                        {
                            "name": "api",
                            "purpose": "rest endpoints",
                            "language": "python",
                            "files": ["server.py"],
                        }
                    ],
                    "dependencies": ["fastapi", "uvicorn"],
                    "constraints_satisfied": {},
                }
            )
        ]
    )


@pytest.fixture
def tmp_board(tmp_path) -> MessageBoard:
    """MessageBoard backed by a per-test SQLite file."""
    db = tmp_path / "mb.sqlite3"
    board = MessageBoard(db)
    yield board
    board.close()
