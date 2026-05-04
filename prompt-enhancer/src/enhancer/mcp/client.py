"""Low-level JSON-RPC 2.0 client for an MCP server over HTTP.

Implements the three MCP methods used by Pass 1 (intent enrichment) and
Pass 3 (rewrite tools):

* ``initialize`` — handshake; returns server capabilities.
* ``tools/list`` — enumerate exposed tools.
* ``tools/call`` — invoke a tool by name with structured arguments.

Scope and constraints
---------------------
* HTTP transport only (POST JSON-RPC). The MCP spec also defines stdio
  and HTTP+SSE transports; both are deferred to v2.1.
* Each :class:`MCPClient` instance owns one :class:`httpx.AsyncClient`
  for the lifetime of the connection. Use :meth:`close` (or the async
  context-manager) to release it.
* Resilience: the three public ``async`` methods are decorated with
  :func:`enhancer.llm.resilience.with_retry`, which consults
  ``self._health`` (a :class:`ProviderHealth` circuit-breaker) and retries
  on transient httpx connection errors, 5xx, and 429 with backoff.
* JSON-RPC errors (``{"error": {"code": ..., "message": ...}}``) are
  surfaced as :class:`MCPError`. They are *not* retried — they are
  protocol-level failures, not transport-level.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

from ..llm.resilience import ProviderHealth, with_retry
from .types import MCPError, ToolInfo


class MCPClient:
    """One MCP server, one HTTP client, one JSON-RPC id sequence."""

    def __init__(self, server_url: str, default_timeout: float = 60.0) -> None:
        self.server_url = server_url.rstrip("/")
        self.default_timeout = default_timeout
        # ``name`` and ``_health`` give the @with_retry decorator the
        # parity contract it expects from ChatProvider implementations.
        self.name = f"mcp:{self.server_url}"
        self._health = ProviderHealth()
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.server_url,
            timeout=default_timeout,
        )
        # Auto-incrementing JSON-RPC id. itertools.count() is thread-safe
        # under CPython's GIL and avoids the overhead of an asyncio.Lock
        # for what is effectively a single-writer counter.
        self._id_seq = itertools.count(1)

    # ── async context-manager ────────────────────────────────────────

    async def __aenter__(self) -> "MCPClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        if not self._client.is_closed:
            await self._client.aclose()

    # ── JSON-RPC envelope helpers ────────────────────────────────────

    def _next_id(self) -> int:
        return next(self._id_seq)

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        """Send a single JSON-RPC request and return the ``result`` field.

        Raises :class:`MCPError` on JSON-RPC error replies; propagates
        :class:`httpx.HTTPStatusError` and connection errors so the
        :func:`with_retry` decorator wrapping the public methods can
        classify and retry them.
        """
        request_id = self._next_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        resp = await self._client.post("/", json=payload)
        # raise_for_status() raises httpx.HTTPStatusError on 4xx/5xx;
        # the @with_retry decorator at the call-site classifies retryable
        # codes (429 / 5xx) vs. fast-fail (other 4xx).
        resp.raise_for_status()
        body = resp.json()

        if "error" in body and body["error"] is not None:
            err = body["error"]
            raise MCPError(
                code=int(err.get("code", -32000)),
                message=str(err.get("message", "unknown MCP error")),
                data=err.get("data"),
            )
        return body.get("result")

    # ── public API ───────────────────────────────────────────────────

    @with_retry(treat_empty_as_failure=False)
    async def initialize(self) -> dict:
        """Send the JSON-RPC ``initialize`` handshake.

        Returns the server's ``capabilities`` block (or the full result
        if capabilities is absent — older servers omit it).
        """
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "prompt-enhancer", "version": "2.0"},
            },
        )
        return result if isinstance(result, dict) else {}

    @with_retry(treat_empty_as_failure=False)
    async def list_tools(self) -> list[ToolInfo]:
        """Enumerate tools exposed by this server.

        Returns parsed :class:`ToolInfo` dataclasses with ``server``
        unset — the registry sets that field when aggregating across
        multiple clients.
        """
        result = await self._rpc("tools/list")
        tools_payload = (result or {}).get("tools", []) if isinstance(result, dict) else []
        out: list[ToolInfo] = []
        for t in tools_payload:
            if not isinstance(t, dict) or "name" not in t:
                continue
            out.append(
                ToolInfo(
                    name=str(t["name"]),
                    description=t.get("description"),
                    input_schema=t.get("inputSchema") or t.get("input_schema"),
                )
            )
        return out

    @with_retry(treat_empty_as_failure=False)
    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Invoke a tool. Returns the raw ``result`` content dict.

        The MCP ``tools/call`` result contains a ``content`` array of
        typed parts (``text`` / ``image`` / ``resource``); we return the
        whole result so callers can inspect ``isError`` and structured
        outputs.
        """
        result = await self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        return result if isinstance(result, dict) else {"content": result}
