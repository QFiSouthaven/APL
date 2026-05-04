"""MCP (Model Context Protocol) client subpackage.

This package exposes a transport-agnostic client surface that the
prompt-enhancer pipeline uses to call out to MCP servers from Pass 1
(intent enrichment) and Pass 3 (rewrite tools).

**Scope (v2.0):** HTTP transport ONLY. The MCP spec
(https://modelcontextprotocol.io) also defines stdio and HTTP+SSE
transports — both are deferred to v2.1. Server URLs must be
``http://...`` or ``https://...``; subprocess-launched stdio servers
are not yet supported.

Public surface:

* :class:`MCPClient` — single-server JSON-RPC over HTTP.
* :class:`MCPRegistry` — multi-server orchestrator with concurrent
  ``tools/list`` fan-out.
* :class:`MCPToolInvoker` — registry adapter that emits the
  ``MCP_TOOL_INVOKED`` / ``MCP_TOOL_RESULT`` pipeline events around
  each call.
* :class:`ToolInfo` — frozen dataclass describing one advertised tool.
* :class:`MCPError` / :class:`MCPTimeoutError` — JSON-RPC failures.
"""

from __future__ import annotations

from .client import MCPClient
from .invoker import MCPToolInvoker
from .registry import MCPRegistry
from .types import MCPError, MCPTimeoutError, ToolInfo

__all__ = [
    "MCPClient",
    "MCPToolInvoker",
    "MCPRegistry",
    "ToolInfo",
    "MCPError",
    "MCPTimeoutError",
]
