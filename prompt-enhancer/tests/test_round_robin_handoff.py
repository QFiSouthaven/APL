"""Tests for the round-robin handoff helper used by the Studio page.

Two layers covered:

* :func:`build_review_request` — body shape contract (pure helper).
* :func:`post_review` — peer-missing path is exercised against a real
  call into the helper (we stub the discovery layer to return ``""``).

The 200/error paths exercise the ``HandoffResult`` plumbing using a
mocked ``httpx.AsyncClient`` so we don't need a live round-robin sibling.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from enhancer.ui.components import round_robin_handoff
from enhancer.ui.components.round_robin_handoff import (
    HandoffResult,
    build_review_request,
    post_review,
)


# ─── build_review_request ───────────────────────────────────────────────


def test_round_robin_request_body_shape() -> None:
    """The POST body must be ``{layer, purpose, files}`` with the enhanced
    text in ``files['enhanced.txt']`` and the original prompt echoed in
    ``purpose`` so the reviewer sees user intent.
    """
    body = build_review_request(
        original_prompt="Make me a chatbot for tax filing",
        enhanced="You are a tax-filing chatbot. ...",
    )

    assert set(body.keys()) == {"layer", "purpose", "files"}
    assert body["layer"] == "prompt"
    assert "Make me a chatbot for tax filing" in body["purpose"]
    assert body["purpose"].startswith("User asked: ")
    assert body["files"] == {"enhanced.txt": "You are a tax-filing chatbot. ..."}


def test_round_robin_request_truncates_long_purpose() -> None:
    """Defensive trim: 50KB user prompts shouldn't bloat the review payload —
    the substantive content is in ``files``."""
    long = "x" * 5000
    body = build_review_request(original_prompt=long, enhanced="ok")
    # 500-char cap + "User asked: " prefix.
    assert len(body["purpose"]) <= 600
    assert body["purpose"].endswith("...")


def test_round_robin_request_handles_empty_inputs() -> None:
    body = build_review_request(original_prompt="", enhanced="")
    assert body["layer"] == "prompt"
    assert body["files"] == {"enhanced.txt": ""}


# ─── post_review — peer missing ────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_robin_handoff_handles_missing_peer(monkeypatch) -> None:
    """When ``get_peer_url`` returns an empty string (peer not in
    services.toml AND no built-in default), :func:`post_review` must
    return ``status='peer_missing'`` without raising or making a network
    call.
    """
    monkeypatch.setattr(
        round_robin_handoff, "get_peer_url",
        lambda name: "",
    )

    result = await post_review(
        original_prompt="hi", enhanced="hello world",
        peer_name="round_robin",
    )

    assert isinstance(result, HandoffResult)
    assert result.status == "peer_missing"
    assert "services.toml" in result.error
    assert result.verdict is None


# ─── post_review — happy path & errors via mocked transport ────────────


class _MockResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = (
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _MockAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` used in the handoff tests.

    Records the URL and body of the POST so we can assert the helper
    targets ``/api/review`` with the expected payload.
    """

    last_url: str | None = None
    last_body: dict | None = None
    response: _MockResponse | None = None
    raise_exc: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    async def __aenter__(self) -> "_MockAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...

    async def post(self, url: str, json: dict | None = None) -> _MockResponse:
        type(self).last_url = url
        type(self).last_body = json
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        assert type(self).response is not None
        return type(self).response


@pytest.fixture(autouse=True)
def _reset_mock_client() -> None:
    _MockAsyncClient.last_url = None
    _MockAsyncClient.last_body = None
    _MockAsyncClient.response = None
    _MockAsyncClient.raise_exc = None


@pytest.mark.asyncio
async def test_round_robin_handoff_ok_returns_verdict(monkeypatch) -> None:
    monkeypatch.setattr(
        round_robin_handoff, "get_peer_url",
        lambda name: "http://127.0.0.1:8766",
    )
    monkeypatch.setattr(
        round_robin_handoff.httpx, "AsyncClient", _MockAsyncClient,
    )
    _MockAsyncClient.response = _MockResponse(
        200,
        {"decision": "PASS", "summary": "looks good", "issues": [],
         "regenerate": False},
    )

    result = await post_review(original_prompt="hi", enhanced="hello")

    assert result.status == "ok"
    assert result.verdict == {
        "decision": "PASS", "summary": "looks good",
        "issues": [], "regenerate": False,
    }
    assert _MockAsyncClient.last_url == "http://127.0.0.1:8766/api/review"
    assert _MockAsyncClient.last_body is not None
    assert _MockAsyncClient.last_body["layer"] == "prompt"


@pytest.mark.asyncio
async def test_round_robin_handoff_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(
        round_robin_handoff, "get_peer_url",
        lambda name: "http://127.0.0.1:8766",
    )
    monkeypatch.setattr(
        round_robin_handoff.httpx, "AsyncClient", _MockAsyncClient,
    )
    _MockAsyncClient.raise_exc = httpx.ConnectError("connection refused")

    result = await post_review(original_prompt="hi", enhanced="hello")

    assert result.status == "unreachable"
    assert "ConnectError" in result.error
    assert result.verdict is None


@pytest.mark.asyncio
async def test_round_robin_handoff_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        round_robin_handoff, "get_peer_url",
        lambda name: "http://127.0.0.1:8766",
    )
    monkeypatch.setattr(
        round_robin_handoff.httpx, "AsyncClient", _MockAsyncClient,
    )
    _MockAsyncClient.response = _MockResponse(503, "service down")

    result = await post_review(original_prompt="hi", enhanced="hello")

    assert result.status == "http_error"
    assert result.http_status == 503
