"""Settings page — provider/model selection + per-run defaults.

v1 reads/writes via env vars (see ``enhancer.config.Settings``); a TOML
settings file lands in v0.2 so the UI can persist preferences without
shell setup.
"""

from __future__ import annotations

from nicegui import ui

from ...config import config_dir, data_dir, load


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

    with ui.column().classes("p-4 gap-3 max-w-[720px]"):
        ui.label("Settings").classes("text-h5 text-white")

        with ui.element("div").classes("studio-card"):
            ui.label("Backend").classes("text-caption text-grey")
            ui.label(f"Provider: {s.provider}").classes("text-body1 text-white")
            ui.label(f"Inference URL: {s.lms_base_url}")
            ui.label(f"Mgmt URL: {s.lms_management_url}")
            ui.label(f"Default model: {s.default_model or '(auto-detect)'}")
            ui.label(f"Scorer model: {s.scorer_model or '(same as default)'}")

        with ui.element("div").classes("studio-card"):
            ui.label("Pipeline defaults").classes("text-caption text-grey")
            ui.label(f"Temperature: {s.temperature}")
            ui.label(f"Max-tokens scale: {s.max_tokens_scale}")
            ui.label(f"Disambiguate threshold: {s.disambiguate_threshold} weakness fields")

        with ui.element("div").classes("studio-card"):
            ui.label("Reliability").classes("text-caption text-grey")
            ui.label(f"Request timeout: {s.request_timeout} s")
            ui.label(f"Idle timeout: {s.idle_timeout} s  "
                     "(do not change — protects against LM Link silent stalls)")

        with ui.element("div").classes("studio-card"):
            ui.label("Paths").classes("text-caption text-grey")
            ui.label(f"Config: {config_dir()}")
            ui.label(f"Data:   {data_dir()}")

        with ui.element("div").classes("studio-card"):
            ui.label("How to change settings").classes("text-caption text-grey")
            ui.markdown(
                "v1 reads from environment variables (prefix `ENHANCER_`). "
                "Examples:\n\n"
                "```\n"
                "set ENHANCER_DEFAULT_MODEL=gptoss-120b-uncensored-hauhaucs-aggressive\n"
                "set ENHANCER_TEMPERATURE=0.5\n"
                "set ENHANCER_LMS_BASE_URL=http://127.0.0.1:1234/v1\n"
                "```\n\n"
                "Restart the Studio after changing env vars. A persisted "
                "TOML settings file lands in v0.2."
            )
