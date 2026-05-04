import httpx
import pytest

from round_robin.lm_client import LMLinkClient, LMLinkError


@pytest.fixture
def mock_transport():
    """Yield a callable that returns a fresh client wired to the given handler."""
    def _make(handler):
        transport = httpx.MockTransport(handler)
        client = LMLinkClient()
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=transport, http2=False,
        )
        return client
    return _make


async def test_models_returns_data(mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [
            {"id": "llama-3.1", "device": "alpha-pc"},
            {"id": "qwen-2.5", "device": "bravo-pc"},
        ]})
    client = mock_transport(handler)
    models = await client.models()
    assert [m["id"] for m in models] == ["llama-3.1", "qwen-2.5"]
    await client.aclose()


async def test_models_http_error_raises(mock_transport):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    client = mock_transport(handler)
    with pytest.raises(LMLinkError):
        await client.models()
    await client.aclose()


async def test_health_returns_false_on_failure(mock_transport):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)
    client = mock_transport(handler)
    assert await client.health() is False
    await client.aclose()


async def test_chat_returns_message_content(mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode().replace(" ", "")
        assert '"stream":false' in body
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello world"}}]
        })
    client = mock_transport(handler)
    text = await client.chat([{"role": "user", "content": "hi"}], model="m")
    assert text == "hello world"
    await client.aclose()


async def test_chat_stream_yields_tokens(mock_transport):
    sse = (
        b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})
    client = mock_transport(handler)
    tokens = []
    async for t in client.chat_stream([{"role": "user", "content": "x"}], model="m"):
        tokens.append(t)
    assert tokens == ["hel", "lo"]
    await client.aclose()


async def test_chat_stream_skips_bad_json(mock_transport):
    sse = (
        b'data: not-json\n\n'
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})
    client = mock_transport(handler)
    tokens = [t async for t in client.chat_stream([{"role": "user", "content": "x"}], model="m")]
    assert tokens == ["ok"]
    await client.aclose()


# ── Regression: production-constructor path must produce absolute URLs ──────


@pytest.mark.asyncio
async def test_chat_stream_uses_absolute_url_in_default_client():
    """LMLinkClient() — default constructor, no transport swap. Catches the
    "Request URL is missing an 'http://' or 'https://' protocol" bug that
    happens when methods use relative paths but base_url isn't set on the
    AsyncClient. respx pins on the absolute URL, so the test fails loudly if
    the request hits a relative one."""
    import respx
    from round_robin.lm_client import LMLinkClient

    sse = (
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    async with respx.mock(assert_all_called=True) as mock:
        # Match the EXACT absolute URL the production code should be sending to.
        route = mock.post("http://localhost:1234/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse,
                headers={"content-type": "text/event-stream"},
            )
        )
        client = LMLinkClient()  # no transport swap
        try:
            tokens = [
                t async for t in client.chat_stream(
                    [{"role": "user", "content": "x"}], model="m"
                )
            ]
        finally:
            await client.aclose()
        assert tokens == ["a"]
        assert route.called


@pytest.mark.asyncio
async def test_chat_uses_absolute_url_in_default_client():
    """Same regression on the non-streaming path."""
    import respx
    from round_robin.lm_client import LMLinkClient

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://localhost:1234/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": "ok", "reasoning_content": ""},
                             "finish_reason": "stop"}],
            })
        )
        client = LMLinkClient()
        try:
            out = await client.chat([{"role": "user", "content": "x"}], model="m")
        finally:
            await client.aclose()
        assert out == "ok"
        assert route.called
