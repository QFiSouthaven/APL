"""Multi-server orchestration for MCP clients.

Pass 1 (intent enrichment) and Pass 3 (rewrite tools) need to talk to
several MCP servers concurrently — e.g. a filesystem server, a search
server, and a weather server. The registry holds those clients by
logical name and fans out ``tools/list`` queries in parallel.

A failure on one server (down, 500, malformed) must NOT block the
others. :meth:`list_all_tools` uses ``asyncio.gather(return_exceptions=True)``
and returns ``[]`` for any server that errored, so the pipeline can
proceed with whatever subset of capabilities is currently available.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .client import MCPClient
from .types import ToolInfo


class MCPRegistry:
    """Map of ``server_name -> MCPClient`` with concurrent fan-out helpers."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}

    # ── client lifecycle ─────────────────────────────────────────────

    def register(self, name: str, server_url: str) -> None:
        """Add a server. Replaces any existing entry under ``name``."""
        if name in self._clients:
            # Best-effort: the old client owns an httpx pool; the caller
            # is expected to await close_all() before re-registering, but
            # if they don't we just drop the reference.
            pass
        self._clients[name] = MCPClient(server_url)

    def unregister(self, name: str) -> None:
        """Remove a server by name. No-op if not present."""
        self._clients.pop(name, None)

    def clients(self) -> dict[str, MCPClient]:
        """Return a shallow copy of the name→client map (read-only view)."""
        return dict(self._clients)

    # ── aggregate operations ─────────────────────────────────────────

    async def list_all_tools(self) -> dict[str, list[ToolInfo]]:
        """Fan out ``tools/list`` to every registered server in parallel.

        Servers that error (timeout, transport, JSON-RPC error) return
        ``[]`` for their slot — the surviving servers' tools still come
        through.
        """
        names = list(self._clients.keys())
        if not names:
            return {}

        coros = [self._clients[name].list_tools() for name in names]
        results = await asyncio.gather(*coros, return_exceptions=True)

        out: dict[str, list[ToolInfo]] = {}
        for name, res in zip(names, results):
            if isinstance(res, BaseException):
                out[name] = []
                continue
            # Stamp the server name onto each ToolInfo so the pipeline
            # knows where to invoke it.
            out[name] = [replace(t, server=name) for t in res]
        return out

    async def invoke(self, server: str, tool: str, args: dict) -> dict:
        """Convenience wrapper: pick the named client and call ``tools/call``."""
        client = self._clients.get(server)
        if client is None:
            raise KeyError(f"no MCP server registered as {server!r}")
        return await client.call_tool(tool, args)

    async def close_all(self) -> None:
        """Close every registered client. Errors during close are swallowed."""
        if not self._clients:
            return
        await asyncio.gather(
            *(c.close() for c in self._clients.values()),
            return_exceptions=True,
        )
