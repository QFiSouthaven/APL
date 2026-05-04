"""Desktop launcher: starts uvicorn in a thread, opens a pywebview window."""
from __future__ import annotations

import socket
import threading
import time
from urllib.parse import urlparse

import uvicorn
import webview

from round_robin.discovery import DEFAULTS, get_peer_url
from round_robin.server import app


def _discovery_port() -> int:
    """Resolve the round-robin port from services.toml (or fall back to default).

    Order:
      1. Read ``%APPDATA%\\swarm\\services.toml`` (or platform equivalent),
         look up the ``round_robin`` entry, parse its port.
      2. Fall back to the port baked into ``discovery.DEFAULTS`` (8766) — this
         must match prompt-enhancer's ``api/discovery.py:DEFAULTS`` so the two
         products agree on each other's locations.
    """
    url = get_peer_url("round_robin", default=DEFAULTS["round_robin"])
    parsed = urlparse(url)
    if parsed.port:
        return parsed.port
    # Final fallback if a malformed override stripped the port.
    return urlparse(DEFAULTS["round_robin"]).port or 8766


def _wait_until_ready(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)


def main() -> None:
    port = _discovery_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_until_ready("127.0.0.1", port)
    webview.create_window("Round Robin", f"http://127.0.0.1:{port}", width=1280, height=820)
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
