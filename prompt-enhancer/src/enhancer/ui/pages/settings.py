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
from ...llm.panel_config import (
    SUPPORTED_PROVIDERS,
    PanelConfig,
    SlotConfig,
    default_config as default_panel_config,
    load_panel_config,
    panel_config_path,
    save_panel_config,
    validate as validate_panel_config,
)
from ...llm.reasoning_panel import VALID_AGGREGATORS, VALID_MODES


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

        # ── Reasoning Panel ────────────────────────────────────────────
        # Off by default — preserves byte-identical v1.x behavior. When
        # enabled, the Studio reads this config at run time and wires a
        # live ReasoningPanel into run_pipeline().
        _render_reasoning_panel_card(s)

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


def _render_reasoning_panel_card(settings) -> None:  # noqa: C901, PLR0915
    """Reasoning Panel card — multi-LLM slot configuration.

    Persists a separate ``panel.toml`` next to ``settings.toml`` (the
    frozen :class:`enhancer.config.Settings` schema deliberately does
    not own this nested-table config). Studio reads the same file at
    run time and builds a live ``ReasoningPanel`` from it.
    """
    cfg = load_panel_config()
    if not cfg.slots:
        # Seed first-time-users with a primary placeholder so they can
        # see the schema. ``enabled=False`` keeps the panel inert until
        # the user toggles it on.
        cfg = default_panel_config()

    state: dict[str, object] = {
        "enabled": bool(cfg.enabled),
        "mode": cfg.mode,
        "aggregator": cfg.aggregator,
        # Slots are mutable rows the table widget owns; copy out of dataclass.
        "slots": [
            {
                "name": s.name,
                "provider": s.provider,
                "model": s.model,
                "base_url": s.base_url,
                "role": s.role,
                "weight": float(s.weight),
            }
            for s in cfg.slots
        ],
    }

    with ui.element("div").classes("studio-card"):
        ui.label("Reasoning Panel").classes("text-caption text-grey")
        ui.label(
            "Optional multi-LLM panel. The first row is the Primary "
            "(canonical output). Add Partners to consult alongside; the "
            "Aggregator decides how their outputs reduce. Off by default."
        ).classes("text-caption text-grey")

        with ui.row().classes("gap-3 items-center"):
            ui.switch("Enable panel").bind_value(state, "enabled")
            ui.select(
                options=sorted(VALID_MODES),
                label="Mode",
            ).bind_value(state, "mode").classes("min-w-[160px]")
            ui.select(
                options=sorted(VALID_AGGREGATORS),
                label="Aggregator",
            ).bind_value(state, "aggregator").classes("min-w-[180px]")

        slots_container = ui.column().classes("w-full gap-2 mt-2")
        status_label = ui.label("").classes("text-caption text-grey")

        # Cached LM Studio model list — populated by "Refresh primary models".
        # Stored on state so each row's model field can offer it as a hint.
        state["lmstudio_models"] = []  # type: ignore[assignment]

        def _render_rows() -> None:
            slots_container.clear()
            slots = state["slots"]  # type: ignore[assignment]
            with slots_container:
                for idx, row in enumerate(slots):  # type: ignore[arg-type]
                    is_primary = idx == 0
                    with ui.row().classes("w-full gap-2 items-center"):
                        if is_primary:
                            ui.label("Primary").classes(
                                "text-caption"
                            ).style("min-width: 70px; color: var(--accent);")
                        else:
                            ui.label(f"Partner {idx}").classes(
                                "text-caption text-grey"
                            ).style("min-width: 70px;")
                        ui.input(label="Name").bind_value(
                            row, "name"
                        ).classes("min-w-[140px]")
                        ui.select(
                            options=list(SUPPORTED_PROVIDERS),
                            label="Provider",
                        ).bind_value(row, "provider").classes("min-w-[120px]")
                        ui.input(label="Model").bind_value(
                            row, "model"
                        ).classes("min-w-[260px]").props("clearable")
                        ui.input(label="Base URL (optional)").bind_value(
                            row, "base_url"
                        ).classes("min-w-[220px]")
                        ui.input(label="Role").bind_value(
                            row, "role"
                        ).classes("min-w-[160px]")
                        ui.number(
                            label="Weight", min=0.0, step=0.1, format="%.2f",
                        ).bind_value(row, "weight").classes("min-w-[100px]")
                        if is_primary:
                            ui.label("").style("min-width: 80px;")
                        else:
                            def _make_remover(i: int):
                                def _remove() -> None:
                                    state["slots"].pop(i)  # type: ignore[attr-defined]
                                    _render_rows()
                                return _remove
                            ui.button(
                                "Remove", icon="delete",
                                on_click=_make_remover(idx),
                            ).props("flat dense color=negative").style(
                                "min-width: 80px;"
                            )

        def _add_partner() -> None:
            n = len(state["slots"])  # type: ignore[arg-type]
            state["slots"].append({  # type: ignore[attr-defined]
                "name": f"partner_{n}",
                "provider": "lmstudio",
                "model": "",
                "base_url": "",
                "role": "",
                "weight": 1.0,
            })
            _render_rows()

        async def _refresh_lmstudio_models() -> None:
            """Hit /v1/models on the active LM Studio host once and cache
            the list. Reported in the status label so users know what they
            can paste into the Model column."""
            from ...llm.lmstudio import LMStudioProvider
            live = load()
            provider = LMStudioProvider(
                base_url=live.lms_base_url,
                management_url=live.lms_management_url,
                default_timeout=10.0,
            )
            try:
                models = await provider.list_models()
            except Exception as exc:  # noqa: BLE001
                status_label.set_text(f"Refresh failed: {exc}")
                return
            state["lmstudio_models"] = models
            if models:
                preview = ", ".join(models[:4])
                more = f" (+{len(models) - 4} more)" if len(models) > 4 else ""
                status_label.set_text(
                    f"LM Studio models: {preview}{more}"
                )
            else:
                status_label.set_text(
                    "No models reported by LM Studio at the active base URL."
                )

        async def _save_panel() -> None:
            cfg_to_save = PanelConfig(
                enabled=bool(state["enabled"]),
                mode=str(state["mode"]),
                aggregator=str(state["aggregator"]),
                slots=[
                    SlotConfig(
                        name=str(r.get("name", "")).strip(),
                        provider=str(r.get("provider", "lmstudio")).strip().lower(),
                        model=str(r.get("model", "")).strip(),
                        base_url=str(r.get("base_url", "")).strip(),
                        role=str(r.get("role", "")).strip(),
                        weight=float(r.get("weight", 1.0)),
                    )
                    for r in state["slots"]  # type: ignore[arg-type]
                ],
            )
            errs = validate_panel_config(cfg_to_save)
            # Validation only blocks save when enabled — a disabled panel
            # with placeholder rows shouldn't refuse to save just because
            # the user hasn't filled in models yet.
            if errs and cfg_to_save.enabled:
                status_label.set_text(
                    "Cannot save: " + " | ".join(errs)
                )
                ui.notify(
                    "Panel config has validation errors — see status line.",
                    type="negative",
                )
                return
            try:
                written = save_panel_config(cfg_to_save)
            except OSError as exc:
                ui.notify(f"Save failed: {exc}", type="negative")
                return
            note = "Reasoning Panel saved"
            if errs:
                note += f" (warnings: {len(errs)})"
            status_label.set_text(f"{note} — {written}")
            ui.notify(note, type="positive")

        with ui.row().classes("gap-2 mt-2"):
            ui.button("+ Add Partner", on_click=_add_partner).props("flat")
            ui.button(
                "Refresh primary models",
                on_click=lambda: ui.timer(0.01, _refresh_lmstudio_models, once=True),
            ).props("flat")
            ui.button("Save panel", on_click=_save_panel).props("color=primary")

        ui.label(f"Panel config file: {panel_config_path()}").classes(
            "text-caption text-grey"
        )

        _render_rows()
