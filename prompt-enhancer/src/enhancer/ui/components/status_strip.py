"""Live pipeline status strip — 9 nodes, color-coded by state.

Mirrors the layout of the redesigned Agent Loop status strip in the
swarm-agent-dev WebUI. Each node has ``data-state`` ∈ {idle, running,
done, error} which the Dark CSS in ``app.py`` styles.
"""

from __future__ import annotations

from nicegui import ui

NODE_LABELS: list[tuple[str, str]] = [
    ("pass1", "Pass 1"),
    ("pass2", "Pass 2"),
    ("focus", "Focus"),
    ("pass3", "Pass 3"),
    ("persona", "Persona"),
    ("pass4", "Pass 4"),
    ("magnitude", "Magnitude"),
    ("sot", "SoT"),
    ("done", "Done"),
]


class StatusStrip:
    """One row of 9 status nodes; ``set(node, state)`` flips colors live."""

    def __init__(self) -> None:
        self._nodes: dict[str, ui.element] = {}
        with ui.row().classes("gap-1 items-center"):
            for key, label in NODE_LABELS:
                with ui.element("div").classes("status-node").props(f'data-state=idle id=node-{key}') as node:
                    ui.element("span").classes("dot")
                    ui.label(label)
                self._nodes[key] = node

    def set(self, key: str, state: str) -> None:
        node = self._nodes.get(key)
        if node is None:
            return
        node.props(f"data-state={state}")

    def reset(self) -> None:
        for n in self._nodes.values():
            n.props("data-state=idle")
