"""Tests for the ``enhancer.mcp`` subpackage.

Coverage:

* :class:`MCPClient` — JSON-RPC envelope correctness, parsing of
  ``initialize`` / ``tools/list`` / ``tools/call`` responses, error
  handling, retry-on-5xx, fast-fail-on-4xx.
* :class:`MCPRegistry` — concurrent fan-out, partial failure handling.
* :class:`MCPToolInvoker` — event emission, EventType-fallback path.

All HTTP traffic is intercepted with ``httpx.MockTransport`` (see
``_patch_httpx`` — same pattern as ``tests/test_lms_discovery.py``).
"""

from __future__ import annotations

import httpx
import pytest

from enhancer.mcp import client as mcp_client_mod
from enhancer.mcp import (
    MCPClient,
    MCPError,
    MCPRegistry,
    MCPToolInvoker,
    ToolInfo,
)


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler):
    """Make every ``httpx.AsyncClient(...)`` route through MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mcp_client_mod.httpx, "AsyncClient", _factory)


def _patch_httpx_per_url(monkeypatch: pytest.MonkeyPatch, handlers: dict):
    """Route each server URL substring to its own handler.

    ``handlers`` maps a URL substring (e.g. ``"server-a"``) to a callable.
    """
    real_client = httpx.AsyncClient

    def router(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for needle, handler in handlers.items():
            if needle in url_str:
                return handler(request)
        raise httpx.ConnectError(f"unrouted: {url_str}")

    transport = httpx.MockTransport(router)

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mcp_client_mod.httpx, "AsyncClient", _factory)


def _ok_jsonrpc(request_id: int, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err_jsonrpc(request_id: int, code: int, message: str):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


# ── MCPClient ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_returns_capabilities(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        import json
        captured["body"] = json.loads(body)
        return httpx.Response(
            200,
            json=_ok_jsonrpc(
                captured["body"]["id"],
                {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
            ),
        )

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        caps = await c.initialize()

    assert caps["capabilities"] == {"tools": {}}
    assert captured["body"]["jsonrpc"] == "2.0"
    assert captured["body"]["method"] == "initialize"
    assert isinstance(captured["body"]["id"], int)


@pytest.mark.asyncio
async def test_list_tools_parses_input_schema(monkeypatch):
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {
            "tools": [
                {
                    "name": "search",
                    "description": "search the web",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
                {"name": "no_schema_no_desc"},
            ],
        }))

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        tools = await c.list_tools()

    assert [t.name for t in tools] == ["search", "no_schema_no_desc"]
    assert tools[0].description == "search the web"
    assert tools[0].input_schema == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }
    assert tools[0].server is None  # set by registry, not client
    assert tools[1].description is None
    assert tools[1].input_schema is None


@pytest.mark.asyncio
async def test_list_tools_accepts_snake_case_input_schema(monkeypatch):
    """Some servers emit ``input_schema`` instead of ``inputSchema``."""
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {
            "tools": [{"name": "t", "input_schema": {"type": "object"}}],
        }))

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        tools = await c.list_tools()
    assert tools[0].input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_list_tools_skips_malformed_entries(monkeypatch):
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {
            "tools": [
                {"name": "good"},
                "not-a-dict",        # skipped
                {"description": "no name"},  # skipped
            ],
        }))

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        tools = await c.list_tools()
    assert [t.name for t in tools] == ["good"]


@pytest.mark.asyncio
async def test_call_tool_envelope(monkeypatch):
    captured: dict = {}

    def handler(request):
        import json
        body = json.loads(request.read())
        captured["body"] = body
        return httpx.Response(200, json=_ok_jsonrpc(body["id"], {
            "content": [{"type": "text", "text": "hi"}],
            "isError": False,
        }))

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        result = await c.call_tool("echo", {"msg": "hi"})

    assert captured["body"]["method"] == "tools/call"
    assert captured["body"]["params"] == {"name": "echo", "arguments": {"msg": "hi"}}
    assert result == {"content": [{"type": "text", "text": "hi"}], "isError": False}


@pytest.mark.asyncio
async def test_jsonrpc_error_raises_mcperror(monkeypatch):
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_err_jsonrpc(rid, -32601, "method not found"))

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        with pytest.raises(MCPError) as ei:
            await c.list_tools()
    assert ei.value.code == -32601
    assert "method not found" in str(ei.value)


@pytest.mark.asyncio
async def test_id_increments_per_call(monkeypatch):
    seen_ids: list[int] = []

    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        seen_ids.append(rid)
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"tools": []}))

    _patch_httpx(monkeypatch, handler)
    async with MCPClient("http://server.example") as c:
        await c.list_tools()
        await c.list_tools()
        await c.list_tools()
    assert seen_ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(monkeypatch):
    """@with_retry classifies 5xx as retryable; verify it gets a second
    chance after a transient failure."""
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["calls"] <= 2:
            return httpx.Response(500, json={"oops": True})
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"tools": []}))

    _patch_httpx(monkeypatch, handler)
    # Speed up retry sleeps so the test stays fast.
    import enhancer.llm.resilience as res
    monkeypatch.setattr(res, "_backoff_delay", lambda *a, **k: 0.0)

    async with MCPClient("http://server.example") as c:
        tools = await c.list_tools()
    assert tools == []
    assert state["calls"] == 3  # 2 failures + 1 success


@pytest.mark.asyncio
async def test_4xx_does_not_retry(monkeypatch):
    """Non-429 4xx is not retryable — should fail fast on first attempt."""
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        return httpx.Response(404, json={"error": "not found"})

    _patch_httpx(monkeypatch, handler)
    import enhancer.llm.resilience as res
    monkeypatch.setattr(res, "_backoff_delay", lambda *a, **k: 0.0)

    async with MCPClient("http://server.example") as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.list_tools()
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_close_is_idempotent(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_ok_jsonrpc(1, {"tools": []}))

    _patch_httpx(monkeypatch, handler)
    c = MCPClient("http://server.example")
    await c.close()
    await c.close()  # second close must not raise


@pytest.mark.asyncio
async def test_async_context_manager_closes(monkeypatch):
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"tools": []}))

    _patch_httpx(monkeypatch, handler)
    c = MCPClient("http://server.example")
    async with c:
        await c.list_tools()
    assert c._client.is_closed


# ── MCPRegistry ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_register_and_clients(monkeypatch):
    # Need a handler so MCPClient constructors don't blow up if anyone
    # accidentally hits the network — they shouldn't here.
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=_ok_jsonrpc(1, {})))

    reg = MCPRegistry()
    reg.register("a", "http://a.example")
    reg.register("b", "http://b.example")
    assert set(reg.clients().keys()) == {"a", "b"}
    reg.unregister("a")
    assert set(reg.clients().keys()) == {"b"}
    reg.unregister("missing")  # no-op
    await reg.close_all()


@pytest.mark.asyncio
async def test_registry_list_all_tools_fans_out(monkeypatch):
    def handler_a(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {
            "tools": [{"name": "a-tool"}],
        }))

    def handler_b(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {
            "tools": [{"name": "b-tool-1"}, {"name": "b-tool-2"}],
        }))

    _patch_httpx_per_url(monkeypatch, {
        "server-a": handler_a,
        "server-b": handler_b,
    })

    reg = MCPRegistry()
    reg.register("a", "http://server-a.example")
    reg.register("b", "http://server-b.example")
    out = await reg.list_all_tools()
    await reg.close_all()

    assert set(out.keys()) == {"a", "b"}
    assert [t.name for t in out["a"]] == ["a-tool"]
    assert [t.name for t in out["b"]] == ["b-tool-1", "b-tool-2"]
    # Registry stamps server name onto each ToolInfo.
    assert all(t.server == "a" for t in out["a"])
    assert all(t.server == "b" for t in out["b"])


@pytest.mark.asyncio
async def test_registry_partial_failure_returns_empty_for_failing_server(monkeypatch):
    def handler_a(request):
        raise httpx.ConnectError("a is down")

    def handler_b(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {
            "tools": [{"name": "survivor"}],
        }))

    _patch_httpx_per_url(monkeypatch, {
        "server-a": handler_a,
        "server-b": handler_b,
    })
    # Make retry sleeps free so the failing server burns through quickly.
    import enhancer.llm.resilience as res
    monkeypatch.setattr(res, "_backoff_delay", lambda *a, **k: 0.0)

    reg = MCPRegistry()
    reg.register("a", "http://server-a.example")
    reg.register("b", "http://server-b.example")
    out = await reg.list_all_tools()
    await reg.close_all()

    assert out["a"] == []
    assert [t.name for t in out["b"]] == ["survivor"]


@pytest.mark.asyncio
async def test_registry_list_all_tools_empty_registry():
    reg = MCPRegistry()
    out = await reg.list_all_tools()
    assert out == {}


@pytest.mark.asyncio
async def test_registry_invoke_unknown_server_raises(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=_ok_jsonrpc(1, {})))
    reg = MCPRegistry()
    with pytest.raises(KeyError):
        await reg.invoke("nope", "tool", {})


# ── MCPToolInvoker ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoker_emits_invoked_and_result(monkeypatch):
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"content": [{"type": "text", "text": "ok"}]}))

    _patch_httpx(monkeypatch, handler)
    reg = MCPRegistry()
    reg.register("srv", "http://server.example")
    invoker = MCPToolInvoker(reg)

    events: list[tuple] = []

    async def on_event(event_type, **kwargs):
        # Normalize event identifier to a string for assertions —
        # accepts either an EventType enum member or a literal string.
        name = getattr(event_type, "value", str(event_type))
        events.append((name, kwargs))

    result = await invoker.invoke_with_events("srv", "echo", {"msg": "hi"}, on_event=on_event)
    await reg.close_all()

    assert result["content"][0]["text"] == "ok"
    assert len(events) == 2

    name0, payload0 = events[0]
    name1, payload1 = events[1]
    assert "invoked" in name0
    assert payload0["server"] == "srv"
    assert payload0["tool"] == "echo"
    assert payload0["args"] == {"msg": "hi"}

    assert "result" in name1
    assert payload1["server"] == "srv"
    assert payload1["tool"] == "echo"
    assert payload1["ok"] is True
    assert isinstance(payload1["duration_ms"], float)
    assert payload1["duration_ms"] >= 0.0


@pytest.mark.asyncio
async def test_invoker_emits_failure_event_on_error(monkeypatch):
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_err_jsonrpc(rid, -32601, "no such tool"))

    _patch_httpx(monkeypatch, handler)
    reg = MCPRegistry()
    reg.register("srv", "http://server.example")
    invoker = MCPToolInvoker(reg)

    events: list[tuple] = []

    async def on_event(event_type, **kwargs):
        name = getattr(event_type, "value", str(event_type))
        events.append((name, kwargs))

    with pytest.raises(MCPError):
        await invoker.invoke_with_events("srv", "ghost", {}, on_event=on_event)
    await reg.close_all()

    # Invoked + failed-result events both emitted before the exception
    # propagated.
    assert len(events) == 2
    _, payload = events[1]
    assert payload["ok"] is False
    assert "error" in payload
    assert "no such tool" in payload["error"] or "32601" in payload["error"]


@pytest.mark.asyncio
async def test_invoker_uses_eventtype_when_present(monkeypatch):
    """When ``EventType.MCP_TOOL_INVOKED`` exists in the enum, the invoker
    emits the enum member (not the fallback literal)."""
    from enhancer.core.events import EventType

    # Sanity check: the enum *should* carry these in current main.
    assert hasattr(EventType, "MCP_TOOL_INVOKED")
    assert hasattr(EventType, "MCP_TOOL_RESULT")

    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"ok": True}))

    _patch_httpx(monkeypatch, handler)
    reg = MCPRegistry()
    reg.register("srv", "http://server.example")
    invoker = MCPToolInvoker(reg)

    received_types: list = []

    async def on_event(event_type, **kwargs):
        received_types.append(event_type)

    await invoker.invoke_with_events("srv", "tool", {}, on_event=on_event)
    await reg.close_all()

    # When the enum member exists, the invoker should emit it.
    assert received_types[0] is EventType.MCP_TOOL_INVOKED
    assert received_types[1] is EventType.MCP_TOOL_RESULT


@pytest.mark.asyncio
async def test_invoker_falls_back_when_eventtype_missing(monkeypatch):
    """Simulate the merge-order-uncertain case where the enum doesn't yet
    have MCP_TOOL_INVOKED — the invoker must emit the literal string
    instead of crashing."""
    from enhancer.core import events as events_mod

    # Build a dummy enum without the MCP members. We patch _resolve_event's
    # source-of-truth by deleting the attributes off a stand-in module.
    class _StubEnum:
        pass

    monkeypatch.setattr(events_mod, "EventType", _StubEnum)

    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"ok": True}))

    _patch_httpx(monkeypatch, handler)
    reg = MCPRegistry()
    reg.register("srv", "http://server.example")
    invoker = MCPToolInvoker(reg)

    received_types: list = []

    async def on_event(event_type, **kwargs):
        received_types.append(event_type)

    await invoker.invoke_with_events("srv", "tool", {}, on_event=on_event)
    await reg.close_all()

    # Fallback path: literal strings.
    assert received_types[0] == "agent.mcp.tool.invoked"
    assert received_types[1] == "agent.mcp.tool.result"


@pytest.mark.asyncio
async def test_invoker_no_callback_still_works(monkeypatch):
    """on_event=None should not raise — invoker just runs the call."""
    def handler(request):
        import json
        rid = json.loads(request.read())["id"]
        return httpx.Response(200, json=_ok_jsonrpc(rid, {"content": []}))

    _patch_httpx(monkeypatch, handler)
    reg = MCPRegistry()
    reg.register("srv", "http://server.example")
    invoker = MCPToolInvoker(reg)
    out = await invoker.invoke_with_events("srv", "t", {}, on_event=None)
    await reg.close_all()
    assert out == {"content": []}


# ── ToolInfo dataclass ──────────────────────────────────────────────


def test_toolinfo_is_frozen():
    t = ToolInfo(name="x")
    with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
        t.name = "y"  # type: ignore[misc]


def test_toolinfo_defaults():
    t = ToolInfo(name="x")
    assert t.description is None
    assert t.input_schema is None
    assert t.server is None
