"""Structured logging + optional OpenTelemetry hooks.

Single entrypoint: :func:`configure_logging`. Idempotent — safe to call
multiple times (the CLI calls it once at startup, the UI calls it once
when the studio page loads, the methodology agent doesn't call it).

structlog is a hard dep (in :mod:`pyproject.toml` ``dependencies``); it
is configured lazily so that simply importing this package does not
mutate any global logging state.

OpenTelemetry is a soft dep — gated entirely on the presence of the
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var. When unset, no OTEL imports run
and there is no overhead. When set, the OTLP exporter is wired and
spans are emitted by ``trace_block`` / ``traced``.

Public surface:

* :func:`configure_logging` — call once at process start.
* :func:`get_logger` — :func:`structlog.get_logger` re-export so
  modules don't have to depend on structlog directly.
* :func:`trace_block` — context manager that yields an OTEL span if
  OTEL is enabled, else a no-op.
* :func:`traced` — function decorator wrapping the body in
  :func:`trace_block`.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
from collections.abc import Iterator
from typing import Any, Callable, TypeVar

import structlog

__all__ = [
    "configure_logging",
    "get_logger",
    "trace_block",
    "traced",
    "is_otel_enabled",
]

_CONFIGURED = False
_OTEL_TRACER: Any = None  # populated lazily when OTEL is enabled

F = TypeVar("F", bound=Callable[..., Any])


def is_otel_enabled() -> bool:
    """OTEL is enabled when the OTLP endpoint env var is set + non-empty."""
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def configure_logging(
    *,
    level: int = logging.INFO,
    json_output: bool | None = None,
) -> None:
    """Configure structlog + stdlib logging for the process.

    Idempotent — repeat calls are no-ops after the first.

    ``json_output`` defaults to ``True`` when stdout is not a TTY
    (machine consumers want JSON), or when ``ENHANCER_LOG_JSON=1``.
    Otherwise renders human-readable colored output.

    OpenTelemetry initialization is automatic when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; nothing happens otherwise.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if json_output is None:
        json_output = (
            os.environ.get("ENHANCER_LOG_JSON", "").strip() in {"1", "true", "yes"}
            or not os.isatty(1)
        )

    # stdlib root logger — let structlog wrap it
    logging.basicConfig(
        level=level,
        format="%(message)s",
        force=False,
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
    ]

    if json_output:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *pre_chain,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _maybe_init_otel()
    _CONFIGURED = True


def _maybe_init_otel() -> None:
    """Wire OpenTelemetry if the env var is set. Soft-fail on any import error."""
    global _OTEL_TRACER
    if not is_otel_enabled():
        return
    try:
        # Imports kept inside the function so the soft dep is truly soft —
        # users without OTEL set never trigger these imports.
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # OTEL libs not installed; silently fall back to no-op.
        return

    resource = Resource.create({SERVICE_NAME: "prompt-enhancer"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _OTEL_TRACER = trace.get_tracer("enhancer")


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger. Re-export so callers don't import structlog."""
    return structlog.get_logger(name) if name else structlog.get_logger()


@contextlib.contextmanager
def trace_block(name: str, **attributes: Any) -> Iterator[Any]:
    """Context manager: yields an OTEL span if enabled, else a no-op.

    Use for measuring sub-operations:

        with trace_block("pass1", model=model):
            ...
    """
    if _OTEL_TRACER is None:
        yield None
        return
    with _OTEL_TRACER.start_as_current_span(name) as span:
        for k, v in attributes.items():
            try:
                span.set_attribute(k, v)
            except Exception:  # never let attribute coercion break a trace
                pass
        yield span


def traced(name: str | None = None) -> Callable[[F], F]:
    """Decorator: wrap a function body in :func:`trace_block`.

    Span name defaults to ``module.qualname`` of the wrapped function.
    """
    def decorate(fn: F) -> F:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_block(span_name):
                return fn(*args, **kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_block(span_name):
                return await fn(*args, **kwargs)

        # Detect coroutine functions properly
        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorate
