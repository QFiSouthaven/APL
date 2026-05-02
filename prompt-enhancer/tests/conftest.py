"""Shared test fixtures.

Most importantly: a ``FakeChatProvider`` that records every call so
concurrency tests can assert ordering and serial execution.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeCall:
    kind: str  # "chat" or "chat_stream"
    model: str
    messages: list[dict]
    started_at: float
    ended_at: float | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass
class FakeChatProvider:
    """Records calls and replays canned responses, with optional latency.

    Use ``responses.append("...")`` to enqueue plain-string responses for
    ``chat()``. Use ``stream_responses.append(["tok1", "tok2", ...])`` to
    enqueue token streams for ``chat_stream()``. ``latency_s`` simulates a
    slow backend so concurrency tests can detect parallelism.
    """

    name: str = "fake"
    latency_s: float = 0.0
    responses: list[str] = field(default_factory=list)
    stream_responses: list[list[str]] = field(default_factory=list)
    calls: list[FakeCall] = field(default_factory=list)
    available_models: list[str] = field(default_factory=lambda: ["fake-7b"])

    async def list_models(self) -> list[str]:
        return list(self.available_models)

    async def context_window(self, model: str) -> int | None:
        return 8192

    async def chat(self, messages, *, model, temperature=None, max_tokens=None,
                   timeout=None) -> str:
        call = FakeCall(
            kind="chat", model=model, messages=list(messages),
            started_at=time.monotonic(),
            temperature=temperature, max_tokens=max_tokens,
        )
        self.calls.append(call)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        call.ended_at = time.monotonic()
        return self.responses.pop(0) if self.responses else ""

    async def chat_stream(self, messages, *, model, temperature=None, max_tokens=None,
                          timeout=None, idle_timeout=120.0) -> AsyncIterator[str]:
        call = FakeCall(
            kind="chat_stream", model=model, messages=list(messages),
            started_at=time.monotonic(),
            temperature=temperature, max_tokens=max_tokens,
        )
        self.calls.append(call)
        tokens = self.stream_responses.pop(0) if self.stream_responses else []
        try:
            for tok in tokens:
                if self.latency_s:
                    await asyncio.sleep(self.latency_s)
                yield tok
        finally:
            call.ended_at = time.monotonic()


@pytest.fixture
def fake_provider() -> FakeChatProvider:
    return FakeChatProvider()


@pytest.fixture
def event_collector():
    """Returns (callback, events_list).  Collected events are inspected after."""
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(event_type, **kwargs):
        # event_type may be EventType enum or str; normalize to str for assertions
        name = getattr(event_type, "value", str(event_type))
        events.append((name, kwargs))

    return on_event, events
