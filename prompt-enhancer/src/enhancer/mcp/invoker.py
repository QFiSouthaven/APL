"""Higher-level MCP invocation adapter for the pipeline.

The pipeline wires events through an ``on_event(event_type, **payload)``
callback (see :mod:`enhancer.core.events`). This module wraps an
:class:`MCPRegistry` so a single ``invoke_with_events`` call also emits
the ``MCP_TOOL_INVOKED`` (before) and ``MCP_TOOL_RESULT`` (after) events
the Studio history view and analytics dashboard depend on.

EventType-name fallback
-----------------------
The two MCP events are v2.0 additions to :class:`enhancer.core.events.EventType`.
A parallel agent owns that enum; depending on merge order they may or
may not exist when this module is imported. Lookups are guarded:

* If ``EventType.MCP_TOOL_INVOKED`` exists, we emit the enum member.
* Otherwise we emit the literal string ``"agent.mcp.tool.invoked"``.

The pipeline's event collector handles both forms (it normalizes via
``getattr(event_type, "value", str(event_type))``), so the caller never
has to branch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .registry import MCPRegistry

# Fallback string literals — used only when the EventType enum does not
# yet carry the v2.0 MCP additions.
_FALLBACK_INVOKED = "agent.mcp.tool.invoked"
_FALLBACK_RESULT = "agent.mcp.tool.result"


def _resolve_event(name: str, fallback: str) -> Any:
    """Return ``EventType.<name>`` if available, else the fallback string.

    Imported lazily so the module still loads cleanly if the ``events``
    module is ever moved or the enum is restructured.
    """
    try:
        from ..core.events import EventType  # local import: avoid cycle
    except Exception:  # pragma: no cover — defensive
        return fallback
    return getattr(EventType, name, fallback)


class MCPToolInvoker:
    """Wrap a registry and emit pipeline events around each tool call."""

    def __init__(self, registry: MCPRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> MCPRegistry:
        return self._registry

    async def invoke_with_events(
        self,
        server: str,
        tool: str,
        args: dict,
        on_event: Callable | None = None,
    ) -> dict:
        """Invoke ``tool`` on ``server`` with ``args``, emitting events.

        Events emitted on ``on_event`` (if provided):

        * ``MCP_TOOL_INVOKED`` before the call — payload: ``server``,
          ``tool``, ``args``.
        * ``MCP_TOOL_RESULT`` after the call — payload: ``server``,
          ``tool``, ``ok`` (bool), ``duration_ms`` (float, 0.1ms
          precision), and on failure ``error`` (string).

        On failure the underlying exception still propagates after the
        result event fires, so callers can distinguish soft failures
        (``ok=False``) from hard ones (exception).
        """
        invoked_evt = _resolve_event("MCP_TOOL_INVOKED", _FALLBACK_INVOKED)
        result_evt = _resolve_event("MCP_TOOL_RESULT", _FALLBACK_RESULT)

        if on_event is not None:
            await _safe_emit(on_event, invoked_evt, server=server, tool=tool, args=args)

        started = time.monotonic()
        try:
            result = await self._registry.invoke(server, tool, args)
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000.0, 1)
            if on_event is not None:
                await _safe_emit(
                    on_event,
                    result_evt,
                    server=server,
                    tool=tool,
                    ok=False,
                    duration_ms=duration_ms,
                    error=repr(exc),
                )
            raise

        duration_ms = round((time.monotonic() - started) * 1000.0, 1)
        if on_event is not None:
            await _safe_emit(
                on_event,
                result_evt,
                server=server,
                tool=tool,
                ok=True,
                duration_ms=duration_ms,
            )
        return result


async def _safe_emit(on_event: Callable, event_type: Any, **payload: Any) -> None:
    """Call ``on_event`` whether it's sync or async. Never raises."""
    try:
        result = on_event(event_type, **payload)
        if hasattr(result, "__await__"):
            await result
    except Exception:  # pragma: no cover — never let a bad listener kill the call
        pass
