"""Cross-umbrella "Live Activity" panel for the Studio page.

A collapsible expansion mounted at the bottom of the Studio page. A
NiceGUI ``ui.timer`` polls every sibling's ``GET /api/activity?limit=20``
every 2 seconds, merges/dedupes the responses by ``(service, ts, type,
summary)``, and renders the top 50 in a scrollable log-style list.

The pure helpers (``merge_events``, ``dedupe_events``) live at module
scope and don't import NiceGUI so unit tests can exercise them without
spinning up a UI.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ...api.discovery import get_peer_url

logger = logging.getLogger(__name__)


# ── pure helpers (unit-testable) ─────────────────────────────────────

# Color palette per sibling. Falls back to grey if a new service ever
# shows up without a matching key.
SERVICE_COLORS: dict[str, str] = {
    "prompt_enhancer": "#3b82f6",  # blue
    "round_robin": "#22c55e",      # green
    "development": "#f97316",      # orange
}
DEFAULT_COLOR = "#9ca3af"          # grey


def merge_events(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a list of ``/api/activity`` responses into a single feed.

    Each response has shape ``{"service": str, "events": [...]}``. We
    flatten with the service tag pinned onto every event, sort by
    timestamp DESC (string-sort works because the format is ISO-8601 +
    fixed precision), then dedupe by ``(service, ts, type, summary)``.
    """
    flat: list[dict[str, Any]] = []
    for resp in responses:
        if not isinstance(resp, dict):
            continue
        service = str(resp.get("service") or "unknown")
        events = resp.get("events") or []
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue
            tagged = dict(ev)
            tagged["service"] = service
            flat.append(tagged)
    # Sort by ts DESC. ts is ISO-8601 + Z so lexicographic == chrono.
    flat.sort(key=lambda e: (e.get("ts") or "", e.get("service") or ""), reverse=True)
    return dedupe_events(flat)


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate events by ``(service, ts, type, summary)``.

    The first occurrence (i.e. the one preserved by the caller's
    sort order) is kept. Used to collapse repeats across overlapping
    polls — when the same poll is fired twice in quick succession,
    sibling A's 200-row buffer would otherwise emit each event 2×.
    """
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        key = (
            str(ev.get("service") or ""),
            str(ev.get("ts") or ""),
            str(ev.get("type") or ""),
            str(ev.get("summary") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _format_hms(ts_iso: str) -> str:
    """Pull the HH:MM:SS portion out of an ISO-8601 timestamp.

    Defensive — if the string isn't shaped right, return whatever
    comes after a 'T' or the original.
    """
    try:
        # "2026-05-04T15:30:14.123Z" -> "15:30:14"
        return ts_iso.split("T", 1)[1][:8]
    except (IndexError, AttributeError):
        return ts_iso or ""


def _service_short(service: str) -> str:
    """Compact 2-3 char tag for a sibling."""
    if service == "prompt_enhancer":
        return "PE"
    if service == "round_robin":
        return "RR"
    if service == "development":
        return "DEV"
    return service[:3].upper() or "?"


# ── async poller ────────────────────────────────────────────────────

async def fetch_one(client: httpx.AsyncClient, url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    """Fetch ``url + /api/activity?limit=20``; return None on failure.

    Silent on connection errors, timeouts, non-200s — the panel's
    "RR unreachable" badge is driven from the None return rather than
    spamming notify() popups every 2 seconds.
    """
    if not url:
        return None
    target = f"{url.rstrip('/')}/api/activity?limit=20"
    try:
        resp = await client.get(target, timeout=timeout)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return None
    except Exception:  # noqa: BLE001 — defensive, never break the poll loop
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    return body


async def fetch_all() -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Poll all 3 siblings; return (merged_events, reachable_per_service).

    ``reachable_per_service`` keys are the sibling names; values are
    True when the last fetch returned a usable body, False otherwise.
    The caller renders an "X unreachable" badge for any False entry.
    """
    targets = {
        "prompt_enhancer": get_peer_url("prompt_enhancer"),
        "round_robin": get_peer_url("round_robin"),
        "development": get_peer_url("development"),
    }
    reachable: dict[str, bool] = {k: False for k in targets}
    bodies: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(fetch_one(client, url) for url in targets.values()),
            return_exceptions=True,
        )
    for (name, _url), result in zip(targets.items(), results):
        if isinstance(result, dict):
            reachable[name] = True
            bodies.append(result)
        # exceptions and None just mean "unreachable" — silent retry.
    return merge_events(bodies), reachable


# ── NiceGUI panel ───────────────────────────────────────────────────

def render_activity_panel() -> None:
    """Render a "Live Activity (umbrella)" expansion at the call site.

    Caller must already be inside a NiceGUI page context. The expansion
    starts collapsed; opening it does NOT change the polling cadence —
    the timer runs regardless so events accrue while the panel is shut.
    """
    # Local import: keep the helpers above usable from tests that don't
    # have NiceGUI installed in their isolated environment.
    from nicegui import ui

    state: dict[str, Any] = {
        "paused": False,
        "events": [],
        "reachable": {"prompt_enhancer": True, "round_robin": True, "development": True},
    }

    with ui.expansion(
        "Live Activity (umbrella)", icon="rss_feed",
    ).classes("w-full"):
        with ui.row().classes("gap-2 items-center w-full"):
            pause_btn = ui.button("Pause", icon="pause").props("flat dense")
            ui.button(
                "Clear",
                icon="delete_sweep",
                on_click=lambda: (state.update(events=[]), _redraw()),
            ).props("flat dense")
            badges_row = ui.row().classes("gap-1 items-center")
        log_box = ui.column().classes("w-full gap-0 mt-1").style(
            "max-height: 280px; overflow-y: auto; "
            "font-family: monospace; font-size: 12px;"
        )

    def _redraw() -> None:
        log_box.clear()
        events = state["events"][:50]
        with log_box:
            if not events:
                ui.label("(no activity yet)").classes("text-caption text-grey")
                return
            for ev in events:
                service = str(ev.get("service") or "unknown")
                color = SERVICE_COLORS.get(service, DEFAULT_COLOR)
                hms = _format_hms(str(ev.get("ts") or ""))
                tag = _service_short(service)
                summary = str(ev.get("summary") or "")
                with ui.row().classes("gap-2 items-baseline w-full no-wrap"):
                    ui.label(f"[{hms}]").style("color: #6b7280;")
                    ui.label(tag).style(
                        f"color: {color}; font-weight: 600; min-width: 28px;"
                    )
                    ui.label(summary).style("color: #d1d5db; flex: 1;").classes(
                        "ellipsis"
                    )

    def _redraw_badges() -> None:
        badges_row.clear()
        with badges_row:
            for name, ok in state["reachable"].items():
                if ok:
                    continue
                ui.label(f"{_service_short(name)} unreachable").classes(
                    "text-caption"
                ).style("color: #f87171;")

    def _toggle_pause() -> None:
        state["paused"] = not state["paused"]
        pause_btn.set_text("Resume" if state["paused"] else "Pause")
        pause_btn.props(f"flat dense icon={'play_arrow' if state['paused'] else 'pause'}")

    pause_btn.on_click(_toggle_pause)

    async def _tick() -> None:
        if state["paused"]:
            return
        try:
            merged, reachable = await fetch_all()
        except Exception:  # noqa: BLE001
            logger.exception("activity panel poll failed")
            return
        state["events"] = merged
        state["reachable"] = reachable
        _redraw()
        _redraw_badges()

    # 2 s cadence per the spec.
    ui.timer(2.0, _tick)
    # Render an initial "(no activity yet)" so the panel isn't empty.
    _redraw()
