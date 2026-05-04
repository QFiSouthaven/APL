"""Optional `lms` CLI wrapper. Never raises — returns None if CLI is missing.

Parses the `lms link status` output, which on a working LM Link setup looks like:

    This device: DESKTOP-NDMJ1VD
    Status: Online

    Found 1 device:

      - m5
        Status: connected
        Identifier: fa59d011d0477706fdd14b238cc5dd44
        Loaded Models Instances:
          - gpt-oss-120b-abliterated-i1
          - some-other-model

When LM Link is disabled or no remote devices are paired, the output is much
shorter ("LM Link not enabled" or just "This device: …" with no devices block).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Known canonical install locations for the lms CLI. shutil.which() respects PATH,
# but PATH is unreliable when our process is launched by a tool that sanitizes env
# (e.g. some IDE preview launchers). These are checked as a fallback.
_FALLBACK_LMS_PATHS = [
    Path.home() / ".lmstudio" / "bin" / "lms.exe",      # Windows
    Path.home() / ".lmstudio" / "bin" / "lms",          # macOS / Linux
    Path("/Applications/LM Studio.app/Contents/Resources/bin/lms"),  # macOS app bundle
]


def _resolve_lms() -> str | None:
    """Find the lms binary via PATH, falling back to known install locations."""
    via_path = shutil.which("lms")
    if via_path:
        return via_path
    for p in _FALLBACK_LMS_PATHS:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None

_THIS_DEVICE_RE = re.compile(r"^This device:\s*(.+?)\s*$", re.IGNORECASE)
_FOUND_N_RE = re.compile(r"^Found\s+(\d+)\s+device", re.IGNORECASE)
_DEVICE_HEADER_RE = re.compile(r"^-\s*(\S.*?)\s*$")            # "  - m5"
_KV_RE = re.compile(r"^([A-Za-z][\w ]+):\s*(.*)$")             # "Status: connected", "Identifier: ...", "Loaded Models Instances:"
_NESTED_LIST_ITEM_RE = re.compile(r"^-\s*(\S.+?)\s*$")          # nested under "Loaded Models Instances:"


async def _run_lms(*args: str, timeout: float = 5.0) -> tuple[str, str] | None:
    """Run `lms <args>` and return (stdout, stderr). None if CLI unavailable."""
    lms_path = _resolve_lms()
    if lms_path is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            lms_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError, NotImplementedError) as exc:
        logger.debug("lms %s failed: %s", " ".join(args), exc)
        return None
    return (
        (stdout or b"").decode("utf-8", errors="replace").strip(),
        (stderr or b"").decode("utf-8", errors="replace").strip(),
    )


async def lms_link_status() -> dict[str, Any] | None:
    """Run `lms link status` and return parsed output, or None if unavailable.

    Prefers `--json` (stable contract since LM Studio 0.3.x) and falls back to
    the legacy text parser if the local `lms` predates that flag.
    """
    # ── Try --json first (stable, structured) ──
    json_result = await _run_lms("link", "status", "--json", timeout=5.0)
    if json_result is not None:
        out_text, err_text = json_result
        text = out_text or err_text
        if text:
            try:
                import json as _json
                payload = _json.loads(text)
                if isinstance(payload, dict):
                    return _normalize_json_link_status(payload)
            except (ValueError, _json.JSONDecodeError):
                # Older lms: --json silently ignored, output is still text — fall through
                pass

    # ── Fallback to text parser ──
    text_result = await _run_lms("link", "status", timeout=5.0)
    if text_result is None:
        return None
    out_text, err_text = text_result
    text = out_text or err_text
    return _parse_link_status(text)


def _normalize_json_link_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert `lms link status --json` shape into the dict the rest of the app
    consumes. The JSON keys may evolve between LM Studio versions, so we look at
    several candidates per field."""
    this_device = (
        payload.get("thisDevice")
        or payload.get("this_device")
        or (payload.get("self") or {}).get("name")
    )
    enabled = bool(
        payload.get("enabled", payload.get("status") in ("Online", "online", True))
    )
    remotes_raw = (
        payload.get("peers")
        or payload.get("devices")
        or payload.get("remoteDevices")
        or payload.get("remote_devices")
        or []
    )
    remote_devices: list[dict[str, Any]] = []
    for entry in remotes_raw:
        if not isinstance(entry, dict):
            continue
        # Filter out the local device if the API returns it in the peer list
        name = entry.get("name") or entry.get("deviceName")
        if name and this_device and name == this_device:
            continue
        loaded = (
            entry.get("loadedModels")
            or entry.get("loaded_models")
            or entry.get("models")
            or []
        )
        if isinstance(loaded, list):
            loaded_names = [
                m if isinstance(m, str) else (m.get("identifier") or m.get("modelKey") or m.get("name"))
                for m in loaded if m
            ]
            loaded_names = [n for n in loaded_names if n]
        else:
            loaded_names = []
        remote_devices.append({
            "name": name,
            "identifier": entry.get("identifier") or entry.get("id"),
            "status": entry.get("status") or ("connected" if entry.get("connected") else None),
            "loaded_models": loaded_names,
        })
    devs = ([this_device] if this_device else []) + [d["name"] for d in remote_devices if d.get("name")]
    return {
        "raw": "<json>",
        "enabled": enabled or bool(this_device),
        "this_device": this_device,
        "remote_devices": remote_devices,
        "devices": devs,
    }


def _parse_link_status(text: str) -> dict[str, Any]:
    """Parse the structured-but-untyped output of `lms link status`.

    Returns a dict with:
        raw            — the raw output string
        enabled        — True if LM Link reported a "Found N device(s)" header,
                         OR a "This device:" line and no obvious "not enabled" message
        this_device    — name of the local machine (str or None)
        remote_devices — list of {name, identifier, status, loaded_models}
        devices        — flat list of device names (this + remotes), kept for
                         backward compatibility with the older parser
    """
    out: dict[str, Any] = {
        "raw": text,
        "enabled": False,
        "this_device": None,
        "remote_devices": [],
        "devices": [],
    }
    if not text:
        return out

    lower = text.lower()
    if "not enabled" in lower or "lm link is disabled" in lower:
        return out

    lines = text.splitlines()

    # 1) extract this_device + the index of "Found N device(s):" header
    found_idx: int | None = None
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if not ln:
            continue
        m = _THIS_DEVICE_RE.match(ln)
        if m:
            out["this_device"] = m.group(1).strip()
            continue
        m = _FOUND_N_RE.match(ln)
        if m:
            found_idx = i
            break

    # 2) walk the remote-devices block
    if found_idx is not None:
        out["enabled"] = True
        i = found_idx + 1
        current: dict[str, Any] | None = None
        in_loaded_models_block = False
        while i < len(lines):
            raw = lines[i]
            ln = raw.strip()
            i += 1
            if not ln:
                in_loaded_models_block = False
                continue
            # Top-level device header — indent doesn't matter, content matches "- <name>"
            header = _DEVICE_HEADER_RE.match(ln) if ":" not in ln else None
            if header and not in_loaded_models_block:
                if current:
                    out["remote_devices"].append(current)
                current = {
                    "name": header.group(1),
                    "identifier": None,
                    "status": None,
                    "loaded_models": [],
                }
                continue
            kv = _KV_RE.match(ln)
            if kv and current:
                key = kv.group(1).strip().lower()
                val = kv.group(2).strip()
                if key == "status":
                    current["status"] = val
                    in_loaded_models_block = False
                elif key == "identifier":
                    current["identifier"] = val
                    in_loaded_models_block = False
                elif key.startswith("loaded model"):
                    # The value is usually empty (key is the header, models follow on next lines)
                    in_loaded_models_block = True
                    if val:
                        current["loaded_models"].append(val)
                else:
                    in_loaded_models_block = False
                continue
            # Nested "  - <model>" items inside the loaded-models block
            if in_loaded_models_block and current:
                item = _NESTED_LIST_ITEM_RE.match(ln)
                if item:
                    current["loaded_models"].append(item.group(1))
                    continue
        if current:
            out["remote_devices"].append(current)
    elif out["this_device"]:
        # No remote devices but this_device known → LM Link is technically up,
        # just nothing paired yet.
        out["enabled"] = True

    # Flat device list (back-compat for any callers reading link.devices)
    devs = []
    if out["this_device"]:
        devs.append(out["this_device"])
    devs.extend(d["name"] for d in out["remote_devices"] if d.get("name"))
    out["devices"] = devs
    return out
