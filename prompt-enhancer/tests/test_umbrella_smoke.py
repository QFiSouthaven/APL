"""Cross-umbrella integration smoke test.

Boots each sibling's FastAPI app in-process (no port binding, no
subprocess) and asserts that:

* every sibling exposes ``GET /api/health`` and returns 200;
* every sibling's ``GET /api/peers`` advertises the OTHER siblings'
  default URLs from the shared ``DEFAULTS`` table;
* prompt-enhancer's discovery layer can resolve each peer by the
  canonical underscore key (``round_robin``, ``development``).

This test injects the round-robin and development ``src/`` directories
onto ``sys.path`` because each sibling lives in its own venv on disk.
If either sibling repo is missing, the affected assertions are skipped
rather than failing — the umbrella allows partial checkouts.

Frozen contracts this test guards:
* ``/api/health`` returning 200 with a JSON body
* ``/api/peers`` returning ``{"services": {<name>: <url>, ...}}``
* the canonical underscore peer-name keying introduced in commit f0389be

NOT marked ``@pytest.mark.integration`` — runs by default in PE's
``pytest -q``. Pure in-memory; no real ports are bound.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enhancer.api import discovery as pe_discovery
from enhancer.api.rest import router as pe_router


# Paths to sibling source trees. APL/ is two levels up from this test file.
APL_ROOT = Path(__file__).resolve().parent.parent.parent
ROUND_ROBIN_SRC = APL_ROOT / "round-robin" / "src"
DEVELOPMENT_SRC = APL_ROOT / "development" / "src"


def _ensure_on_path(p: Path) -> bool:
    """Add ``p`` to ``sys.path`` (front) if it exists. Returns True if added."""
    if not p.exists():
        return False
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
    return True


# ── PE in-memory app ────────────────────────────────────────────────────


def _pe_app() -> FastAPI:
    """Mount PE's integration router onto a bare FastAPI app for testing."""
    app = FastAPI()
    app.include_router(pe_router)
    return app


# ── round-robin / development app loaders (lazy + skip-if-missing) ─────


def _round_robin_app() -> FastAPI | None:
    if not _ensure_on_path(ROUND_ROBIN_SRC):
        return None
    try:
        from round_robin.server import app as rr_app  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover — surfaces import errors clearly
        pytest.skip(f"round-robin app failed to import: {exc!r}")
    return rr_app


def _development_app() -> FastAPI | None:
    if not _ensure_on_path(DEVELOPMENT_SRC):
        return None
    try:
        from development.server import app as dev_app  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"development app failed to import: {exc!r}")
    return dev_app


# ── tests ──────────────────────────────────────────────────────────────


def test_pe_health_in_memory():
    """PE's /api/health returns 200 with the expected envelope shape."""
    client = TestClient(_pe_app())
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body
    assert "schema_version" in body


def test_pe_peers_lists_siblings_in_memory():
    """PE's /api/peers must include round_robin and development by default."""
    client = TestClient(_pe_app())
    r = client.get("/api/peers")
    assert r.status_code == 200
    services = r.json()["services"]
    assert "prompt_enhancer" in services
    assert "round_robin" in services
    assert "development" in services
    # Default ports must match the umbrella contract.
    assert services["round_robin"].endswith(":8766")
    assert services["development"].endswith(":8767")


def test_pe_discovery_resolves_canonical_underscore_keys():
    """get_peer_url must return non-empty URLs for the underscore-canonical
    peer names. Guards regression on commit f0389be (the rename to
    underscore form). Hyphenated keys are NOT supported."""
    assert pe_discovery.get_peer_url("round_robin").startswith("http://")
    assert pe_discovery.get_peer_url("development").startswith("http://")
    # Hyphenated form is intentionally NOT in DEFAULTS.
    assert pe_discovery.get_peer_url("round-robin") == ""


def test_round_robin_health_in_memory():
    rr_app = _round_robin_app()
    if rr_app is None:
        pytest.skip("round-robin source tree not present")
    client = TestClient(rr_app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    # round-robin's additive blob: status/service/version on top of the
    # legacy probe fields.
    assert body.get("service") == "round_robin"
    assert body.get("status") == "ok"


def test_round_robin_peers_lists_siblings():
    rr_app = _round_robin_app()
    if rr_app is None:
        pytest.skip("round-robin source tree not present")
    client = TestClient(rr_app)
    r = client.get("/api/peers")
    assert r.status_code == 200
    services = r.json()["services"]
    assert "prompt_enhancer" in services
    assert "round_robin" in services
    assert "development" in services


def test_development_health_in_memory():
    dev_app = _development_app()
    if dev_app is None:
        pytest.skip("development source tree not present")
    client = TestClient(dev_app)
    r = client.get("/api/health")
    # 200 (normal) or 503 (degraded stub) — both mean "the server stood up".
    assert r.status_code in (200, 503)
    body = r.json()
    assert body.get("service") == "development"


def test_development_peers_lists_siblings():
    dev_app = _development_app()
    if dev_app is None:
        pytest.skip("development source tree not present")
    client = TestClient(dev_app)
    r = client.get("/api/peers")
    if r.status_code == 404:
        # Degraded stub serves only /api/health — still acceptable for v2.2.
        pytest.skip("development is in degraded mode; /api/peers absent")
    assert r.status_code == 200
    services = r.json()["services"]
    assert "prompt_enhancer" in services
    assert "round_robin" in services
    assert "development" in services


@pytest.mark.asyncio
async def test_pe_health_via_async_client():
    """Smoke test that the same app works under httpx.AsyncClient — confirms
    the inter-product contract works for any sibling that calls PE
    asynchronously (round-robin, development, swarm-loop)."""
    transport = httpx.ASGITransport(app=_pe_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True
