"""Tests for `enhancer.llm.resilience`.

Covers retry exhaustion, success-after-retry, no-retry on 4xx (except
429), Retry-After header respect, empty-content retry, circuit-breaker
open/close, and stream-aware retry (setup-only).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from enhancer.llm import resilience
from enhancer.llm.resilience import (
    ProviderHealth,
    ProviderUnhealthyError,
    get_session_stats,
    reset_session_stats,
    with_retry,
    with_stream_retry,
)


@pytest.fixture(autouse=True)
def _zero_stats():
    """Each test starts with clean session counters."""
    reset_session_stats()
    yield
    reset_session_stats()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Speed up tests — replace asyncio.sleep with a no-op."""
    async def fast(_):
        return None
    monkeypatch.setattr(resilience.asyncio, "sleep", fast)


# ── helpers ─────────────────────────────────────────────────────────


def _http_status_error(code: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    req = httpx.Request("POST", "http://x/")
    resp = httpx.Response(code, headers=headers, request=req)
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


class _FakeProvider:
    """Drop-in for testing — has the surface the decorators expect."""

    name = "fake"

    def __init__(self):
        self._health = ProviderHealth(threshold=3, cooldown_secs=30.0)
        self.calls = 0


# ── chat() retry decorator ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_succeeds_first_try():
    class P(_FakeProvider):
        @with_retry()
        async def chat(self, *_, **__):
            self.calls += 1
            return "ok"

    p = P()
    result = await p.chat()
    assert result == "ok"
    assert p.calls == 1
    assert get_session_stats()["retries"] == 0
    assert get_session_stats()["recoveries"] == 0


@pytest.mark.asyncio
async def test_chat_succeeds_after_retries():
    class P(_FakeProvider):
        @with_retry()
        async def chat(self, *_, **__):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ConnectError("refused")
            return "ok"

    p = P()
    result = await p.chat()
    assert result == "ok"
    assert p.calls == 3
    stats = get_session_stats()
    assert stats["retries"] == 2
    assert stats["recoveries"] == 1


@pytest.mark.asyncio
async def test_chat_retry_exhaustion_raises():
    class P(_FakeProvider):
        @with_retry(max_retries=2)
        async def chat(self, *_, **__):
            self.calls += 1
            raise httpx.ConnectError("refused")

    p = P()
    with pytest.raises(httpx.ConnectError):
        await p.chat()
    assert p.calls == 3  # 1 initial + 2 retries
    assert get_session_stats()["failures"] == 1


@pytest.mark.asyncio
async def test_chat_no_retry_on_4xx():
    class P(_FakeProvider):
        @with_retry()
        async def chat(self, *_, **__):
            self.calls += 1
            raise _http_status_error(400)

    p = P()
    with pytest.raises(httpx.HTTPStatusError):
        await p.chat()
    assert p.calls == 1
    assert get_session_stats()["retries"] == 0
    assert get_session_stats()["failures"] == 1


@pytest.mark.asyncio
async def test_chat_retries_on_429_with_retry_after(monkeypatch):
    """429 with Retry-After header should retry AND honor the header."""
    sleeps: list[float] = []

    async def capture_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(resilience.asyncio, "sleep", capture_sleep)

    class P(_FakeProvider):
        @with_retry(max_retries=2)
        async def chat(self, *_, **__):
            self.calls += 1
            if self.calls == 1:
                raise _http_status_error(429, retry_after="2.5")
            return "recovered"

    p = P()
    result = await p.chat()
    assert result == "recovered"
    assert p.calls == 2
    # First retry slept exactly Retry-After seconds, NOT the backoff value.
    assert sleeps == [2.5]


@pytest.mark.asyncio
async def test_chat_retries_on_5xx():
    class P(_FakeProvider):
        @with_retry(max_retries=2)
        async def chat(self, *_, **__):
            self.calls += 1
            if self.calls == 1:
                raise _http_status_error(503)
            return "ok"

    p = P()
    assert await p.chat() == "ok"
    assert p.calls == 2


@pytest.mark.asyncio
async def test_chat_empty_content_retries():
    """Empty string returns count as failure when treat_empty_as_failure=True."""
    class P(_FakeProvider):
        @with_retry(max_retries=2, treat_empty_as_failure=True)
        async def chat(self, *_, **__):
            self.calls += 1
            if self.calls < 3:
                return ""
            return "got it"

    p = P()
    result = await p.chat()
    assert result == "got it"
    assert p.calls == 3
    assert get_session_stats()["retries"] == 2


@pytest.mark.asyncio
async def test_chat_empty_content_returns_after_exhaustion():
    """When all retries return empty, decorator returns the empty string."""
    class P(_FakeProvider):
        @with_retry(max_retries=1, treat_empty_as_failure=True)
        async def chat(self, *_, **__):
            self.calls += 1
            return ""

    p = P()
    result = await p.chat()
    assert result == ""
    assert p.calls == 2
    assert get_session_stats()["failures"] == 1


# ── circuit breaker ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold():
    """3 consecutive final failures → circuit OPEN; next call short-circuits."""
    class P(_FakeProvider):
        @with_retry(max_retries=0)
        async def chat(self, *_, **__):
            self.calls += 1
            raise httpx.ConnectError("refused")

    p = P()
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await p.chat()
    assert p.calls == 3
    assert p._health.is_open

    # Next call doesn't even invoke the provider.
    with pytest.raises(ProviderUnhealthyError):
        await p.chat()
    assert p.calls == 3


@pytest.mark.asyncio
async def test_circuit_closes_after_cooldown():
    """When cooldown elapses, the next call probes; success closes the circuit."""
    class P(_FakeProvider):
        def __init__(self):
            super().__init__()
            # Cooldown well above the Windows scheduler tick (~15.6ms) so
            # the post-sleep assertion is not racing against timer
            # resolution. The cost is ~50ms of test wall-clock.
            self._health = ProviderHealth(threshold=2, cooldown_secs=0.02)
            self.fail_count = 0

        @with_retry(max_retries=0)
        async def chat(self, *_, **__):
            self.calls += 1
            if self.fail_count > 0:
                self.fail_count -= 1
                raise httpx.ConnectError("refused")
            return "ok"

    p = P()
    p.fail_count = 2  # fail twice → opens circuit
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await p.chat()
    assert p._health.is_open

    # Wait well past cooldown so the assertion is not flaky on Windows.
    time.sleep(0.05)
    assert not p._health.is_open

    # Probe call succeeds, circuit closes.
    result = await p.chat()
    assert result == "ok"
    assert p._health.consecutive_failures == 0


# ── stream retry ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_retries_setup_failure():
    """Connection error before first chunk → retry; eventual success yields."""
    attempts = {"n": 0}

    class P(_FakeProvider):
        @with_stream_retry(max_retries=2)
        async def chat_stream(self, *_, **__):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise httpx.ConnectError("refused")
            yield "a"
            yield "b"

    p = P()
    chunks = [c async for c in p.chat_stream()]
    assert chunks == ["a", "b"]
    assert attempts["n"] == 2
    assert get_session_stats()["recoveries"] == 1


@pytest.mark.asyncio
async def test_stream_no_retry_after_first_chunk():
    """Once the stream has yielded once, errors propagate to caller."""
    class P(_FakeProvider):
        @with_stream_retry(max_retries=3)
        async def chat_stream(self, *_, **__):
            yield "a"
            raise httpx.ReadTimeout("stalled")

    p = P()
    collected: list[str] = []
    with pytest.raises(httpx.ReadTimeout):
        async for c in p.chat_stream():
            collected.append(c)
    assert collected == ["a"]


@pytest.mark.asyncio
async def test_stream_no_retry_on_4xx_setup():
    class P(_FakeProvider):
        @with_stream_retry(max_retries=3)
        async def chat_stream(self, *_, **__):
            raise _http_status_error(400)
            yield ""  # unreachable; makes this an async generator

    p = P()
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in p.chat_stream():
            pass
    assert get_session_stats()["failures"] == 1


@pytest.mark.asyncio
async def test_stream_circuit_open_short_circuits():
    class P(_FakeProvider):
        def __init__(self):
            super().__init__()
            self._health = ProviderHealth(threshold=1, cooldown_secs=30.0)

        @with_stream_retry(max_retries=0)
        async def chat_stream(self, *_, **__):
            raise httpx.ConnectError("refused")
            yield ""

    p = P()
    # First call → opens circuit.
    with pytest.raises(httpx.ConnectError):
        async for _ in p.chat_stream():
            pass
    assert p._health.is_open

    # Second call → fast-fail.
    with pytest.raises(ProviderUnhealthyError):
        async for _ in p.chat_stream():
            pass


# ── ProviderHealth state machine ────────────────────────────────────


def test_health_records_success_resets_counter():
    h = ProviderHealth(threshold=3)
    h.record_failure()
    h.record_failure()
    h.record_success()
    assert h.consecutive_failures == 0
    assert not h.is_open


def test_health_opens_at_threshold():
    h = ProviderHealth(threshold=3, cooldown_secs=30)
    h.record_failure()
    h.record_failure()
    assert not h.is_open
    h.record_failure()
    assert h.is_open
