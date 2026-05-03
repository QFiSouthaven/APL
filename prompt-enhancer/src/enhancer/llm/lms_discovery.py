"""LM Studio model discovery + auto-load.

LM Studio's OpenAI-compatible ``/v1`` surface exposes inference but not
model management (``/v1/models/load`` does not exist — it returns
``{"error":"Unexpected endpoint or method"}``). Loading must go through
either the LM Studio desktop UI or the ``lms`` CLI shell-out, which is
how this module does it.

Public surface:

* :func:`discover_chat_models` — GET ``/api/v0/models``, return
  chat-capable entries with state (loaded vs not-loaded).
* :func:`ensure_model_loaded` — return the id of a chat-capable model
  that is currently loaded, loading one via ``lms load`` if necessary.
  Raises :class:`ModelLoadUnavailableError` with operator instructions
  if no path works.

This is intentionally separate from :mod:`enhancer.llm.lmstudio` so the
``ChatProvider`` layer stays narrow and the discovery/load surface can
be swapped (e.g. for Ollama's ``/api/pull`` later).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("enhancer.llm.lms_discovery")

DEFAULT_MGMT_URL = "http://localhost:1234"

# LM Studio's `/api/v0/models` returns a `type` field per model. Anything
# in CHAT_TYPES can serve `/v1/chat/completions`; anything in
# NON_CHAT_TYPES is filtered out (embeddings, whisper, etc.).
CHAT_TYPES = frozenset({"llm", "vlm"})
NON_CHAT_TYPES = frozenset({"embeddings", "embedding", "whisper", "audio"})

# Common install paths for the `lms` CLI on Windows; checked in order
# after PATH lookup. Extend if LM Studio adds a new install location.
_LMS_CLI_CANDIDATES: list[Path] = [
    Path.home() / ".lmstudio" / "bin" / "lms.exe",
    Path.home() / ".lmstudio" / "bin" / "lms",
]


class ModelLoadUnavailableError(RuntimeError):
    """LM Studio is reachable but no chat-capable model is loaded
    and we cannot auto-load one (no ``lms`` CLI, or load returned non-zero).

    The message always carries an operator instruction so the error
    surface in CLI/UI is actionable.
    """


@dataclass(frozen=True)
class ModelInfo:
    """One row from ``/api/v0/models`` filtered to chat-capable entries."""

    id: str
    type: str  # 'llm' | 'vlm'
    state: str  # 'loaded' | 'not-loaded'
    max_context: int | None = None
    loaded_context: int | None = None

    @property
    def is_loaded(self) -> bool:
        return self.state == "loaded"


async def discover_chat_models(
    base_url: str = DEFAULT_MGMT_URL,
    timeout: float = 5.0,
) -> list[ModelInfo]:
    """GET ``/api/v0/models``; return chat-capable entries, loaded first.

    Returns an empty list on connection error or non-2xx — callers must
    treat empty as "LM Studio unreachable" rather than "no models".
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/v0/models")
            r.raise_for_status()
            data = r.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("discover_chat_models: %s", exc)
        return []

    out: list[ModelInfo] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        mtype = entry.get("type", "")
        if not mid or mtype in NON_CHAT_TYPES or mtype not in CHAT_TYPES:
            continue
        out.append(
            ModelInfo(
                id=mid,
                type=mtype,
                state=entry.get("state", "not-loaded"),
                max_context=entry.get("max_context_length"),
                loaded_context=entry.get("loaded_context_length"),
            )
        )

    # Loaded first, then alphabetical. Stable sort.
    out.sort(key=lambda m: (not m.is_loaded, m.id))
    return out


def find_lms_cli() -> Path | None:
    """Locate the ``lms`` binary. Returns ``None`` if not installed."""
    on_path = shutil.which("lms")
    if on_path:
        return Path(on_path)
    for candidate in _LMS_CLI_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


async def _load_via_cli(model_id: str, timeout: float = 90.0) -> tuple[bool, str]:
    """Shell out to ``lms load <model_id> --gpu max -y``.

    Returns ``(success, stderr_tail)``. Errors are caught and reported in
    the second tuple element so the caller can surface them in a
    :class:`ModelLoadUnavailableError`.
    """
    lms = find_lms_cli()
    if not lms:
        return False, "`lms` CLI not found on PATH or in ~/.lmstudio/bin"
    try:
        proc = await asyncio.create_subprocess_exec(
            str(lms), "load", model_id, "--gpu", "max", "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-300:]
            return False, f"`lms load` exited rc={proc.returncode}: {tail}"
        return True, ""
    except asyncio.TimeoutError:
        return False, f"`lms load {model_id}` timed out after {timeout}s"
    except OSError as exc:
        return False, f"`lms load` spawn failed: {exc}"


async def ensure_model_loaded(
    preferred: str | None = None,
    base_url: str = DEFAULT_MGMT_URL,
) -> str:
    """Return the id of a chat-capable model that is currently loaded.

    Resolution order:

    1. Discover models via ``/api/v0/models``.
    2. If ``preferred`` is supplied AND that model is loaded → return it.
    3. Else if any chat model is loaded → return its id.
    4. Else attempt ``lms load <pick>`` where ``<pick>`` is ``preferred``
       or the first not-loaded chat model. Re-poll; if a model now
       shows loaded, return its id.
    5. Else raise :class:`ModelLoadUnavailableError` with an actionable
       message ("Open LM Studio → load any chat model").
    """
    models = await discover_chat_models(base_url)
    if not models:
        raise ModelLoadUnavailableError(
            f"LM Studio is unreachable at {base_url} or has no chat-capable "
            "models. Open LM Studio and download / load any chat model."
        )

    loaded = [m for m in models if m.is_loaded]
    if preferred:
        for m in loaded:
            if m.id == preferred:
                return m.id
    if loaded:
        return loaded[0].id

    pick = preferred or models[0].id
    logger.info("ensure_model_loaded: nothing loaded, attempting lms load %s", pick)
    ok, err = await _load_via_cli(pick)
    if not ok:
        raise ModelLoadUnavailableError(
            f"No chat model is loaded and auto-load failed. {err}\n"
            "Open LM Studio → load any chat model, then retry."
        )

    # Re-poll to confirm.
    models = await discover_chat_models(base_url)
    loaded = [m for m in models if m.is_loaded]
    if not loaded:
        raise ModelLoadUnavailableError(
            f"`lms load {pick}` returned 0 but no model is reporting loaded. "
            "Open LM Studio and verify the load."
        )
    return loaded[0].id
