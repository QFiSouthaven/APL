"""Pre-flight health probe combining /v1/models and `lms link status`.

The OpenAI-compatible /v1/models endpoint returns a flat list of every model
visible to LM Studio — including remote ones surfaced via LM Link — but with
no per-model host tag. So we cross-reference with `lms link status`'s
"Loaded Models Instances" section to know which models live on which device.
"""
from __future__ import annotations

from typing import Any

from .lm_client import LMLinkClient, LMLinkError
from .lms_cli import lms_link_status


async def probe(client: LMLinkClient) -> dict[str, Any]:
    """Return reachability + discovered models + LM Link metadata."""
    reachable = await client.health()
    models: list[dict[str, Any]] = []
    error: str | None = None
    if reachable:
        try:
            models = await client.models()
        except LMLinkError as exc:
            error = str(exc)
    link = await lms_link_status()
    local_device = (link or {}).get("this_device")
    remote_model_map = _build_remote_model_map(link)
    return {
        "reachable": reachable,
        "models": [_summarize_model(m, local_device, remote_model_map) for m in models],
        "link": link,
        "local_device": local_device,
        "error": error,
    }


def _build_remote_model_map(link: dict[str, Any] | None) -> dict[str, str]:
    """Return {model_id: device_name} for every model loaded on a remote device."""
    out: dict[str, str] = {}
    if not link:
        return out
    for dev in link.get("remote_devices") or []:
        name = dev.get("name") or ""
        if not name:
            continue
        for model_id in dev.get("loaded_models") or []:
            if model_id:
                out[model_id] = name
    return out


def _summarize_model(
    m: dict[str, Any],
    local_device: str | None,
    remote_model_map: dict[str, str],
) -> dict[str, Any]:
    """Tag each model with `is_local` + `device` based on LM Link metadata.

    Resolution order:
    1. Model id matches a remote device's loaded_models → it lives there
    2. Otherwise it's local (or LM Link is off and everything is local)
    """
    model_id = m.get("id") or ""
    remote_device = remote_model_map.get(model_id)
    if remote_device:
        return {
            "id": model_id,
            "owned_by": m.get("owned_by"),
            "device": remote_device,
            "is_local": False,
            "loaded": m.get("loaded"),
            "raw": m,
        }
    # Fall back to any inline tags on the model entry itself, then to local
    inline_device = m.get("device") or m.get("host") or m.get("link_device")
    is_local = inline_device is None or (local_device is not None and inline_device == local_device)
    return {
        "id": model_id,
        "owned_by": m.get("owned_by"),
        "device": inline_device or local_device,
        "is_local": is_local,
        "loaded": m.get("loaded"),
        "raw": m,
    }
