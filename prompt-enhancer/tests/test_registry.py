"""Tests for ``enhancer.llm.registry`` — name lookup + entry-point discovery.

The third-party path is exercised by monkeypatching
``importlib.metadata.entry_points`` against the registry module — we
don't actually pip-install anything.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from enhancer.config import Settings
from enhancer.llm import registry as registry_module
from enhancer.llm.base import ChatProvider
from enhancer.llm.lmstudio import LMStudioProvider
from enhancer.llm.registry import get_provider


# ── helpers ─────────────────────────────────────────────────────────


class _StubProvider(ChatProvider):
    """A minimal ChatProvider subclass used as a "third-party" plugin."""

    name = "stub"

    async def list_models(self) -> list[str]:  # pragma: no cover — unused
        return []

    async def chat(self, messages, *, model, temperature=None,
                   max_tokens=None, timeout=120.0) -> str:  # pragma: no cover
        return ""

    async def chat_stream(self, messages, *, model, temperature=None,
                          max_tokens=None, timeout=600.0,
                          idle_timeout=120.0) -> AsyncIterator[str]:
        # async generator literal: yield nothing.
        if False:  # pragma: no cover — sentinel
            yield ""


class _NotAProvider:
    """Deliberately not a ChatProvider subclass."""

    pass


@dataclass
class _FakeEntryPoint:
    name: str
    target: object  # what ``.load()`` returns

    def load(self):
        return self.target


def _patch_entry_points(monkeypatch, eps):
    """Replace ``_iter_entry_points`` so the registry sees ``eps``.

    Bypasses the importlib.metadata compat shim — that shim is covered
    by its own targeted test below.
    """
    monkeypatch.setattr(
        registry_module, "_iter_entry_points",
        lambda group: list(eps) if group == "enhancer.providers" else [],
    )


# ── built-in lookups ────────────────────────────────────────────────


def test_get_provider_lmstudio_returns_lmstudio_provider():
    settings = Settings(provider="lmstudio")
    p = get_provider(settings)
    assert isinstance(p, LMStudioProvider)


def test_get_provider_unknown_raises_value_error():
    settings = Settings(provider="totally-made-up-name")
    with pytest.raises(ValueError) as ei:
        get_provider(settings)
    msg = str(ei.value)
    assert "totally-made-up-name" in msg
    assert "lmstudio" in msg  # supported list still shown


# ── third-party entry-points ────────────────────────────────────────


def test_get_provider_returns_third_party_via_entry_point(monkeypatch):
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="myllm", target=_StubProvider),
    ])
    settings = Settings(provider="myllm")
    p = get_provider(settings)
    assert isinstance(p, _StubProvider)


def test_entry_point_with_non_chatprovider_is_skipped(monkeypatch, caplog):
    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="bogus", target=_NotAProvider),
    ])
    settings = Settings(provider="bogus")
    with caplog.at_level("WARNING", logger="enhancer.llm.registry"):
        with pytest.raises(ValueError):
            get_provider(settings)
    assert any(
        "not a ChatProvider subclass" in rec.message
        for rec in caplog.records
    )


def test_entry_point_load_failure_falls_through_to_value_error(monkeypatch, caplog):
    class _ExplodingEP:
        name = "kaboom"

        def load(self):
            raise RuntimeError("plugin import broken")

    _patch_entry_points(monkeypatch, [_ExplodingEP()])
    settings = Settings(provider="kaboom")
    with caplog.at_level("WARNING", logger="enhancer.llm.registry"):
        with pytest.raises(ValueError):
            get_provider(settings)
    assert any("Failed to load entry-point" in rec.message
               for rec in caplog.records)


def test_entry_point_with_mismatched_name_is_ignored(monkeypatch):
    """The registry should only consult the entry-point whose ``.name``
    matches ``settings.provider`` — others are skipped without trying
    to instantiate them."""
    instantiated: list[str] = []

    class _Tracking(ChatProvider):
        name = "tracking"

        def __init__(self):
            instantiated.append("tracking")

        async def list_models(self):  # pragma: no cover
            return []

        async def chat(self, messages, *, model, temperature=None,
                       max_tokens=None, timeout=120.0):  # pragma: no cover
            return ""

        async def chat_stream(self, messages, *, model, temperature=None,
                              max_tokens=None, timeout=600.0,
                              idle_timeout=120.0):  # pragma: no cover
            if False:
                yield ""

    _patch_entry_points(monkeypatch, [
        _FakeEntryPoint(name="tracking", target=_Tracking),
    ])
    with pytest.raises(ValueError):
        get_provider(Settings(provider="something-else"))
    assert instantiated == []  # never instantiated


# ── compat shim for importlib.metadata.entry_points ─────────────────


def test_iter_entry_points_uses_group_kwarg_when_supported(monkeypatch):
    """On 3.10+ the modern API takes a ``group=`` keyword."""
    sentinel = [_FakeEntryPoint(name="x", target=_StubProvider)]

    def fake_entry_points(*, group):
        assert group == "enhancer.providers"
        return sentinel

    monkeypatch.setattr(registry_module._im, "entry_points", fake_entry_points)
    out = registry_module._iter_entry_points("enhancer.providers")
    assert list(out) == sentinel


def test_iter_entry_points_falls_back_when_group_kwarg_unsupported(monkeypatch):
    """If the modern kwarg form raises TypeError, fall back to dict-style."""
    sentinel = [_FakeEntryPoint(name="x", target=_StubProvider)]

    class _DictLike:
        def get(self, key, default=None):
            assert key == "enhancer.providers"
            return sentinel

    def fake_entry_points(*args, **kwargs):
        if "group" in kwargs:
            raise TypeError("legacy API: no 'group' kwarg")
        return _DictLike()

    monkeypatch.setattr(registry_module._im, "entry_points", fake_entry_points)
    out = registry_module._iter_entry_points("enhancer.providers")
    assert list(out) == sentinel
