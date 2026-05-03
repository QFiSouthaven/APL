"""NiceGUI Desktop Studio — entry point + page routing.

Pages live under ``enhancer.ui.pages``. This module glues them to the
NiceGUI app, sets up the dark theme, and runs the server.

Launched via ``enhancer ui`` once the user has installed the ``ui``
extra: ``pip install prompt-enhancer[ui]``.
"""

from __future__ import annotations


_DARK_CSS = """
:root {
    --bg: #0f1115;
    --bg-2: #161a22;
    --bg-3: #1f2530;
    --text: #e6e9ef;
    --text-2: #9aa3b2;
    --accent: #5aa9ff;
    --accent-2: #7d6cff;
    --good: #5dd29c;
    --warn: #ffb86b;
    --bad: #ff7177;
    --border: #2a3140;
}
body { background: var(--bg); color: var(--text); }
.studio-card {
    background: var(--bg-2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    margin: 8px 0;
}
.status-node { display: inline-flex; gap: 6px; align-items: center;
               padding: 2px 8px; border-radius: 12px;
               background: var(--bg-3); border: 1px solid var(--border);
               font-size: 0.75rem; color: var(--text-2); }
.status-node[data-state=running] { color: var(--accent);
    border-color: var(--accent); animation: pulse-dot 1.5s infinite; }
.status-node[data-state=done]    { color: var(--good); border-color: var(--good); }
.status-node[data-state=error]   { color: var(--bad); border-color: var(--bad); }
@keyframes pulse-dot { 50% { opacity: 0.55; } }
.dot { width: 8px; height: 8px; border-radius: 50%;
       background: currentColor; }
.score-chip { padding: 1px 8px; border-radius: 10px;
              background: var(--bg-3); margin-right: 4px;
              font-size: 0.75rem; }
"""


def _preflight_lms_warning() -> None:
    """Quick non-blocking check; print a stdout warning if LM Studio
    is unreachable or has no chat model loaded. Never raises."""
    import asyncio

    from ..llm.lms_discovery import discover_chat_models

    try:
        models = asyncio.run(discover_chat_models(timeout=2.0))
    except Exception:
        return  # never block startup
    if not models:
        print("[startup] LM Studio is unreachable — start LM Studio for inference.")
        return
    if not any(m.is_loaded for m in models):
        print(
            "[startup] LM Studio is up but no chat model is loaded.\n"
            "[startup]   Open LM Studio → load any chat model, "
            "or run `lms load <id>`."
        )


def run() -> None:
    """Boot NiceGUI on the configured host:port and open the browser."""
    from nicegui import app as nicegui_app, ui

    from ..api.rest import router as integration_router
    from ..config import load
    from .pages import analytics as analytics_page
    from .pages import compare as compare_page
    from .pages import history as history_page
    from .pages import settings as settings_page
    from .pages import studio as studio_page
    from .pages import templates as templates_page

    _preflight_lms_warning()
    s = load()

    # Mount the inter-product REST API onto NiceGUI's FastAPI app so
    # sibling products (round-robin, interpreter, swarm-loop) can hit
    # /api/enhance, /api/health, /api/peers without importing this pkg.
    nicegui_app.include_router(integration_router)

    # NiceGUI 3.x requires head/body html injection INSIDE each @ui.page
    # route rather than at module scope — global ui.add_head_html during
    # ui.run() with shared=False raises RuntimeError. Each route calls
    # _inject_dark_styles() first thing.
    def _inject_dark_styles() -> None:
        ui.add_head_html(f"<style>{_DARK_CSS}</style>")

    @ui.page("/")
    def _root():
        _inject_dark_styles()
        studio_page.render()

    @ui.page("/history")
    def _history():
        _inject_dark_styles()
        history_page.render()

    @ui.page("/analytics")
    def _analytics():
        _inject_dark_styles()
        analytics_page.render()

    @ui.page("/compare")
    def _compare():
        _inject_dark_styles()
        compare_page.render()

    @ui.page("/templates")
    def _templates():
        _inject_dark_styles()
        templates_page.render()

    @ui.page("/settings")
    def _settings():
        _inject_dark_styles()
        settings_page.render()

    ui.run(
        host=s.ui_host, port=s.ui_port,
        title="Prompt Enhancer", reload=False, show=True, dark=True,
        favicon="🪄",
    )
