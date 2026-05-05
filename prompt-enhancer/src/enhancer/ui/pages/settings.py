"""Settings page — provider/model selection + per-run defaults.

Eight keys are editable from the UI and persisted to a TOML file via
``POST /api/settings`` (handled by ``enhancer.api.rest.update_settings``):

    provider, lms_base_url, lms_management_url, default_model,
    scorer_model, temperature, max_tokens_scale, disambiguate_threshold

The remaining keys (``idle_timeout``, ``request_timeout``, ``ui_host``,
``ui_port``, ``methodology_agent_enabled``) are boot-only or frozen and
are shown as read-only labels. Edit them via env vars or by hand-editing
the TOML file printed at the bottom of the page.
"""

from __future__ import annotations

import httpx
from nicegui import ui

from ...config import config_dir, data_dir, load, settings_path
from ...llm.host_picker import apply_host_pick, parse_hosts


def _sidebar() -> None:
    with ui.left_drawer(value=True, fixed=True).classes("bg-[var(--bg-2)] p-4"):
        ui.label("🪄  Prompt Enhancer").classes("text-h6 text-white")
        ui.separator().classes("my-2")
        ui.link("Studio", "/").classes("text-white")
        ui.link("History", "/history").classes("text-white")
        ui.link("Analytics", "/analytics").classes("text-white")
        ui.link("Compare", "/compare").classes("text-white")
        ui.link("Templates", "/templates").classes("text-white")
        ui.link("Settings", "/settings").classes("text-white")


def render() -> None:
    _sidebar()
    s = load()

    # Mutable state for editable fields. NiceGUI inputs ``.bind_value()``
    # update this dict; the Save button serializes it to JSON.
    state: dict[str, object] = {
        "provider": s.provider,
        "lms_base_url": s.lms_base_url,
        "lms_management_url": s.lms_management_url,
        "default_model": s.default_model,
        "scorer_model": s.scorer_model,
        "temperature": float(s.temperature),
        "max_tokens_scale": float(s.max_tokens_scale),
        "disambiguate_threshold": int(s.disambiguate_threshold),
    }

    with ui.column().classes("p-4 gap-3 max-w-[720px]"):
        ui.label("Settings").classes("text-h5 text-white")

        with ui.element("div").classes("studio-card"):
            ui.label("Backend").classes("text-caption text-grey")
            ui.select(
                options=["lmstudio", "ollama", "openai", "anthropic"],
                label="Provider",
            ).bind_value(state, "provider").classes("w-full")
            ui.input("Inference URL (lms_base_url)").bind_value(
                state, "lms_base_url"
            ).classes("w-full")
            ui.input("Management URL (lms_management_url)").bind_value(
                state, "lms_management_url"
            ).classes("w-full")
            ui.input("Default model").bind_value(state, "default_model").classes("w-full")
            ui.input("Scorer model (blank = same as default)").bind_value(
                state, "scorer_model"
            ).classes("w-full")

        with ui.element("div").classes("studio-card"):
            ui.label("Multi-host (LM Studio LAN discovery)").classes(
                "text-caption text-grey"
            )
            ui.label(
                "One host per line — e.g. http://127.0.0.1:1234/v1. "
                "Click 'Pick best host now' to probe each host and route "
                "inference to the first one that has a chat model loaded."
            ).classes("text-caption text-grey")
            hosts_state = {"hosts": ""}
            ui.textarea(
                placeholder=(
                    "http://127.0.0.1:1234/v1\nhttp://192.168.1.50:1234/v1"
                ),
            ).bind_value(hosts_state, "hosts").classes("w-full").props("rows=4")
            picker_status = ui.label("").classes("text-caption")

            async def _pick_host() -> None:
                hosts = parse_hosts(str(hosts_state["hosts"]))
                if not hosts:
                    picker_status.set_text("Add at least one host above first.")
                    ui.notify("No hosts to probe", type="warning")
                    return
                picker_status.set_text(f"Probing {len(hosts)} host(s)…")
                host, model = await apply_host_pick(hosts)
                if host:
                    picker_status.set_text(
                        f"Active LM Studio host: {host} (loaded model: {model})"
                    )
                    ui.notify(f"Routing to {host}", type="positive")
                else:
                    picker_status.set_text(
                        "No host responded with a loaded chat model — "
                        "active URL unchanged."
                    )
                    ui.notify("No host responded", type="warning")

            ui.button("Pick best host now", on_click=_pick_host).classes("w-full")

        with ui.element("div").classes("studio-card"):
            ui.label("Pipeline defaults").classes("text-caption text-grey")
            ui.label().bind_text_from(
                state, "temperature", lambda v: f"Temperature: {float(v):.2f}"
            )
            ui.slider(min=0.0, max=2.0, step=0.05).bind_value(
                state, "temperature"
            ).classes("w-full")
            ui.label().bind_text_from(
                state, "max_tokens_scale", lambda v: f"Max-tokens scale: {float(v):.2f}"
            )
            ui.slider(min=0.3, max=3.0, step=0.05).bind_value(
                state, "max_tokens_scale"
            ).classes("w-full")
            ui.number(
                label="Disambiguate threshold (weakness fields)",
                min=1, step=1, format="%d",
            ).bind_value(state, "disambiguate_threshold").classes("w-full")

        with ui.element("div").classes("studio-card"):
            ui.label("Reliability (read-only — boot-time)").classes("text-caption text-grey")
            ui.label(f"Request timeout: {s.request_timeout} s")
            ui.label(f"Idle timeout: {s.idle_timeout} s  "
                     "(do not change — protects against LM Link silent stalls)")
            ui.label(f"UI host: {s.ui_host}")
            ui.label(f"UI port: {s.ui_port}")
            ui.label(f"Methodology agent: "
                     f"{'enabled' if s.methodology_agent_enabled else 'disabled'}")

        with ui.element("div").classes("studio-card"):
            ui.label("Paths").classes("text-caption text-grey")
            ui.label(f"Config: {config_dir()}")
            ui.label(f"Data:   {data_dir()}")
            ui.label(f"Settings file: {settings_path()}")

        async def _save() -> None:
            try:
                int_threshold = int(state["disambiguate_threshold"])
            except (TypeError, ValueError):
                ui.notify("Disambiguate threshold must be a whole number", type="negative")
                return
            payload = {
                "provider": str(state["provider"]),
                "lms_base_url": str(state["lms_base_url"]),
                "lms_management_url": str(state["lms_management_url"]),
                "default_model": str(state["default_model"]),
                "scorer_model": str(state["scorer_model"]),
                "temperature": float(state["temperature"]),
                "max_tokens_scale": float(state["max_tokens_scale"]),
                "disambiguate_threshold": int_threshold,
            }
            host, port = s.ui_host, s.ui_port
            url = f"http://{host}:{port}/api/settings"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    body = resp.json()
                    ui.notify(f"Saved to {body.get('path', settings_path())}",
                              type="positive")
                else:
                    detail = resp.text
                    try:
                        detail = resp.json().get("detail", detail)
                    except ValueError:
                        pass
                    ui.notify(f"Save failed ({resp.status_code}): {detail}",
                              type="negative")
            except httpx.HTTPError as exc:
                ui.notify(f"Save failed: {exc}", type="negative")

        ui.button("Save", on_click=_save).classes("w-full")
