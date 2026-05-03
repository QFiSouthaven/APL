"""Provider-layer resilience: retry + circuit breaker + structured logging.

Sits one level below the pipeline. The pipeline still calls
``provider.chat`` and ``provider.chat_stream`` — both methods are now
decorated to retry on transient connection-class errors, fast-fail when
the circuit breaker is open, and emit structlog events the UI can
display via :func:`get_session_stats`.

**Critical:** retry happens at the provider layer, NEVER at the
pipeline layer. The three frozen pipeline invariants (Pass 1→2 serial,
Pass 4 awaited before Magnitude/SoT, ``idle_timeout=120``) are not
disturbed; they remain regression-tested in
``tests/test_concurrency.py``.

Public surface:

* :func:`with_retry` — decorator for ``chat()`` (string return).
* :func:`with_stream_retry` — decorator for ``chat_stream()`` (async
  generator). Retries the connection / first-chunk phase only; once
  the first chunk has yielded, errors propagate.
* :class:`ProviderHealth` — circuit-breaker state attached to each
  provider instance as ``self._health``.
* :class:`ProviderUnhealthyError` — fast-fail when the circuit is open.
* :func:`get_session_stats` / :func:`reset_session_stats` —
  module-scoped counters surfaced to the Studio UI.

The retry / streaming wrap respects the existing pipeline timing
invariants: it does NOT issue retries that would extend a per-call
``idle_timeout`` window — connection timeouts do retry, but a slow
remote-GPU stall surfaces as a retry of the connection only, not of
the inner stream.
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

import httpx
import structlog

logger = structlog.get_logger("enhancer.llm.resilience")

# Module-scoped counters. One process = one session. The Studio's
# session drawer reads these via get_session_stats() and renders them
# beside the active session.
_session_stats: dict[str, int] = {"retries": 0, "failures": 0, "recoveries": 0}


def get_session_stats() -> dict[str, int]:
    """Return a snapshot of the session counters."""
    return dict(_session_stats)


def reset_session_stats() -> None:
    """Zero the session counters. Used by tests + the UI 'New session' button."""
    _session_stats["retries"] = 0
    _session_stats["failures"] = 0
    _session_stats["recoveries"] = 0


class ProviderUnhealthyError(RuntimeError):
    """Circuit-breaker open — fast-fail without invoking the provider."""


class ProviderHealth:
    """Per-instance circuit breaker state.

    After ``threshold`` consecutive failed calls (post-retry), the breaker
    enters OPEN state for ``cooldown_secs``. While OPEN, every call
    fails fast with :class:`ProviderUnhealthyError`. The first success
    after cooldown closes the circuit and resets the counter.
    """

    def __init__(self, threshold: int = 3, cooldown_secs: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown_secs = cooldown_secs
        self.consecutive_failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at > self.cooldown_secs:
            # Auto-close after cooldown — next call is the half-open probe.
            return False
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.opened_at = time.monotonic()


# ── retryability classification ─────────────────────────────────────


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether an exception should trigger a retry."""
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Honor a ``Retry-After`` header on 429 responses if present."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        ra = exc.response.headers.get("retry-after")
        if ra:
            try:
                return max(0.0, float(ra))
            except ValueError:
                return None
    return None


def _backoff_delay(
    attempt: int, base: float, ceiling: float, jitter: float
) -> float:
    """Exponential backoff with ±jitter%, capped at ``ceiling``."""
    raw = min(base * (2 ** (attempt - 1)), ceiling)
    return max(0.0, raw * (1 + random.uniform(-jitter, jitter)))


# ── decorators ──────────────────────────────────────────────────────

T = TypeVar("T")


def with_retry(
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.25,
    treat_empty_as_failure: bool = True,
):
    """Decorator for async ``chat()``-style methods returning a string.

    Retries on transient connection errors, 5xx, 429 (with Retry-After
    honored), and — when ``treat_empty_as_failure`` is ``True`` — on
    empty content responses (the case that bit us in Pass 4 against
    reasoning-token models).
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            health: ProviderHealth | None = getattr(self, "_health", None)
            provider_name = getattr(self, "name", "?")
            if health is not None and health.is_open:
                raise ProviderUnhealthyError(
                    f"{provider_name} circuit OPEN; cooling down "
                    f"({health.cooldown_secs}s)"
                )

            attempt = 0
            while True:
                attempt += 1
                try:
                    result = await fn(self, *args, **kwargs)
                except Exception as exc:
                    if not _is_retryable(exc) or attempt > max_retries:
                        if health is not None:
                            health.record_failure()
                        _session_stats["failures"] += 1
                        logger.error(
                            "llm_call_failed",
                            attempt=attempt, provider=provider_name,
                            error=repr(exc),
                            exhausted=attempt > max_retries,
                        )
                        raise
                    delay = (
                        _retry_after_seconds(exc)
                        or _backoff_delay(attempt, base_delay, max_delay, jitter)
                    )
                    logger.info(
                        "llm_retry",
                        attempt=attempt, provider=provider_name,
                        error=repr(exc), delay=delay,
                    )
                    _session_stats["retries"] += 1
                    await asyncio.sleep(delay)
                    continue

                # Success path. Empty-content handling.
                if (
                    treat_empty_as_failure
                    and isinstance(result, str)
                    and not result.strip()
                ):
                    if attempt > max_retries:
                        if health is not None:
                            health.record_failure()
                        _session_stats["failures"] += 1
                        logger.warning(
                            "llm_call_failed",
                            attempt=attempt, provider=provider_name,
                            error="empty_content_exhausted",
                        )
                        return result  # let caller see the empty
                    delay = _backoff_delay(attempt, base_delay, max_delay, jitter)
                    logger.info(
                        "llm_retry",
                        attempt=attempt, provider=provider_name,
                        error="empty_content", delay=delay,
                    )
                    _session_stats["retries"] += 1
                    await asyncio.sleep(delay)
                    continue

                if attempt > 1:
                    logger.info(
                        "llm_call_recovered",
                        attempt=attempt, provider=provider_name,
                    )
                    _session_stats["recoveries"] += 1
                if health is not None:
                    health.record_success()
                return result

        return wrapped

    return decorate


def with_stream_retry(
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.25,
):
    """Decorator for async-generator ``chat_stream()``-style methods.

    Retries the connection / first-chunk setup only. Once the wrapped
    generator yields its first chunk, errors propagate to the caller —
    we cannot replay a half-consumed stream.
    """

    def decorate(fn: Callable[..., AsyncIterator[Any]]) -> Callable[..., AsyncIterator[Any]]:
        @functools.wraps(fn)
        async def wrapped(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            health: ProviderHealth | None = getattr(self, "_health", None)
            provider_name = getattr(self, "name", "?")
            if health is not None and health.is_open:
                raise ProviderUnhealthyError(
                    f"{provider_name} circuit OPEN; cooling down "
                    f"({health.cooldown_secs}s)"
                )

            attempt = 0
            while True:
                attempt += 1
                gen = fn(self, *args, **kwargs)
                try:
                    first = await gen.__anext__()
                except StopAsyncIteration:
                    # Empty stream — treated like empty_content.
                    if attempt > max_retries:
                        if health is not None:
                            health.record_failure()
                        _session_stats["failures"] += 1
                        logger.warning(
                            "llm_call_failed",
                            attempt=attempt, provider=provider_name,
                            error="empty_stream_exhausted",
                        )
                        return
                    delay = _backoff_delay(attempt, base_delay, max_delay, jitter)
                    logger.info(
                        "llm_retry",
                        attempt=attempt, provider=provider_name,
                        error="empty_stream", delay=delay,
                    )
                    _session_stats["retries"] += 1
                    await asyncio.sleep(delay)
                    continue
                except Exception as exc:
                    if not _is_retryable(exc) or attempt > max_retries:
                        if health is not None:
                            health.record_failure()
                        _session_stats["failures"] += 1
                        logger.error(
                            "llm_call_failed",
                            attempt=attempt, provider=provider_name,
                            error=repr(exc),
                            exhausted=attempt > max_retries,
                        )
                        raise
                    delay = (
                        _retry_after_seconds(exc)
                        or _backoff_delay(attempt, base_delay, max_delay, jitter)
                    )
                    logger.info(
                        "llm_retry",
                        attempt=attempt, provider=provider_name,
                        error=repr(exc), delay=delay,
                    )
                    _session_stats["retries"] += 1
                    await asyncio.sleep(delay)
                    continue

                if attempt > 1:
                    logger.info(
                        "llm_call_recovered",
                        attempt=attempt, provider=provider_name,
                    )
                    _session_stats["recoveries"] += 1
                if health is not None:
                    health.record_success()
                yield first
                async for chunk in gen:
                    yield chunk
                return

        return wrapped

    return decorate
