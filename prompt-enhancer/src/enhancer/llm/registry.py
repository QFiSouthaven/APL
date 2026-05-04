"""Provider + transform registry — discover by name, load by config.

Two entry-point groups are honored (v1.2: providers, v2.0: transforms):

Providers (``enhancer.providers``)::

    [project.entry-points."enhancer.providers"]
    myllm = "my_pkg.provider:MyLLMProvider"

The class must subclass :class:`enhancer.llm.base.ChatProvider`.
:func:`get_provider` consults this group before raising ``ValueError``.

Transforms (``enhancer.transforms``)::

    [project.entry-points."enhancer.transforms"]
    my_xform = "my_pkg.transform:MyTransform"

The class can either be callable (``cls(prompt, **context)``) or expose
an ``.apply(prompt, **context)`` method. :func:`discover_transforms`
returns a dict of registered classes; pipeline wiring of these plugins
is a v2.x follow-up — v2.0 ships the discovery surface only so plugin
authors can publish without waiting for the wiring layer.
"""

from __future__ import annotations

import importlib.metadata as _im
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .base import ChatProvider

if TYPE_CHECKING:
    from ..config import Settings


_log = logging.getLogger(__name__)


def _iter_entry_points(group: str) -> Iterable[Any]:
    """Compat shim for ``importlib.metadata.entry_points``.

    Python 3.10+ supports the ``group=`` keyword on ``entry_points()``,
    but the underlying return type and behavior tightened over 3.10 →
    3.12. The selectable / dict-style access from 3.9 is also still
    in the wild via shimmed backports. This helper tries the modern
    ``group=`` form first and falls back to the older
    ``entry_points()[group]`` form if the kwarg raises ``TypeError``
    (or anything else — better to fall through than to crash startup).
    """
    try:
        eps = _im.entry_points(group=group)
        return list(eps)
    except TypeError:
        # Older API: entry_points() returns a dict-like keyed by group.
        try:
            eps_all = _im.entry_points()
            return list(eps_all.get(group, []))  # type: ignore[union-attr]
        except Exception:  # pragma: no cover — defensive
            return []
    except Exception:  # pragma: no cover — defensive
        return []


def get_provider(settings: Settings) -> ChatProvider:
    """Return the ChatProvider implied by ``settings.provider``."""
    name = settings.provider.lower().strip()
    if name == "lmstudio":
        from .lmstudio import LMStudioProvider
        return LMStudioProvider(
            base_url=settings.lms_base_url,
            management_url=settings.lms_management_url,
            default_timeout=settings.request_timeout,
        )
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider()
    if name == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider()
    if name == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider()

    # Third-party providers via the ``enhancer.providers`` entry-point group.
    for ep in _iter_entry_points("enhancer.providers"):
        if getattr(ep, "name", None) != name:
            continue
        try:
            cls = ep.load()
        except Exception as exc:
            _log.warning(
                "Failed to load entry-point %r in group 'enhancer.providers': %s",
                name, exc,
            )
            continue
        if not (isinstance(cls, type) and issubclass(cls, ChatProvider)):
            _log.warning(
                "Entry-point %r in group 'enhancer.providers' is not a "
                "ChatProvider subclass; skipping.",
                name,
            )
            continue
        try:
            return cls()
        except Exception as exc:
            _log.warning(
                "Failed to instantiate third-party provider %r: %s",
                name, exc,
            )
            continue

    raise ValueError(
        f"Unknown provider: {settings.provider!r}. "
        f"Supported: lmstudio, ollama, openai, anthropic."
    )


def discover_transforms() -> dict[str, type]:
    """Return third-party transforms registered under ``enhancer.transforms``.

    The returned dict maps the entry-point name to the loaded class. Each
    entry is duck-checked: it must be callable OR expose an ``.apply``
    attribute. Anything that fails the check is logged and skipped — never
    raised.

    Pipeline integration of these transforms is a v2.x follow-up; this
    function is the discovery surface so plugin authors can publish a
    package and verify it shows up here without waiting on enhancer's
    own pipeline wiring.
    """
    found: dict[str, type] = {}
    for ep in _iter_entry_points("enhancer.transforms"):
        ep_name = getattr(ep, "name", None)
        if not ep_name:
            continue
        try:
            cls = ep.load()
        except Exception as exc:
            _log.warning(
                "Failed to load entry-point %r in group 'enhancer.transforms': %s",
                ep_name, exc,
            )
            continue
        if not (callable(cls) or hasattr(cls, "apply")):
            _log.warning(
                "Entry-point %r in group 'enhancer.transforms' is neither "
                "callable nor exposes .apply; skipping.",
                ep_name,
            )
            continue
        found[ep_name] = cls
    return found
