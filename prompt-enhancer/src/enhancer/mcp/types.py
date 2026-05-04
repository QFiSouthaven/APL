"""Type definitions for the MCP client subpackage.

Lives in its own module so :class:`ToolInfo` can be imported by both
:mod:`enhancer.mcp.client` and :mod:`enhancer.mcp.registry` without a
circular import.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolInfo:
    """A single MCP tool advertised by a server's ``tools/list`` response.

    The ``server`` field is populated by :class:`MCPRegistry` when it
    aggregates results across multiple servers — individual
    :class:`MCPClient` instances leave it as ``None`` because they have
    no logical name for themselves (just a URL).
    """

    name: str
    description: str | None = None
    input_schema: dict | None = None  # JSON-Schema object (per MCP spec)
    server: str | None = None


class MCPError(RuntimeError):
    """A JSON-RPC ``error`` reply from an MCP server.

    Carries the spec ``code`` and ``message`` so callers can distinguish
    e.g. a missing tool (-32601 method not found) from a server-side
    crash (-32603 internal error).
    """

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPTimeoutError(MCPError):
    """Raised when an MCP request exceeds the configured timeout.

    Distinct from a generic :class:`MCPError` so callers can implement
    timeout-specific fallback policies (e.g. cache stale results) without
    swallowing protocol errors.
    """

    def __init__(self, message: str = "MCP request timed out") -> None:
        super().__init__(code=-32000, message=message)
