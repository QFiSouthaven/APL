"""Branch tree — visualize a run and its children/parent.

A run can be forked from any completed pass via ``parent_run_id`` +
``parent_pass`` (schema in ``persistence/schema.sql``). This component
renders the immediate ancestors and descendants of a given run as a
collapsible tree using NiceGUI's ``ui.tree``.
"""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from ...persistence.db import connect


def render_branch_tree(db_path: Path, run_id: str) -> None:
    """Render the branching context around ``run_id``.

    Shows: lineage (parent chain) → current run (highlighted) → children.
    Clicking any node loads its export in a notify-style preview.
    """
    rows = _load_lineage(db_path, run_id)
    if not rows:
        ui.label("No lineage data — this run has no parent and no children.").classes(
            "text-caption text-grey"
        )
        return

    # Build a tree rooted at the topmost ancestor
    nodes_by_id = {r["id"]: r for r in rows}
    children_by_parent: dict[str | None, list[dict]] = {}
    for r in rows:
        children_by_parent.setdefault(r.get("parent_run_id"), []).append(r)

    roots = [r for r in rows if r.get("parent_run_id") not in nodes_by_id]

    def _to_tree(node: dict) -> dict:
        return {
            "id": node["id"],
            "label": _label_for(node, current=node["id"] == run_id),
            "children": [
                _to_tree(c) for c in children_by_parent.get(node["id"], [])
            ],
        }

    nodes = [_to_tree(r) for r in roots]
    tree = ui.tree(nodes, label_key="label", node_key="id").props(
        "dense default-expand-all"
    ).classes("w-full")
    tree.on("update:selected", lambda e: _on_select(e, nodes_by_id))


def _on_select(e, nodes_by_id: dict[str, dict]) -> None:
    sel = e.args
    selected = sel[0] if isinstance(sel, list) and sel else sel
    node = nodes_by_id.get(selected) if selected else None
    if not node:
        return
    ui.notify(
        f"{node['id']}  ·  {node.get('task_type', '?')}  ·  "
        f"+{node.get('improvement', '—')}%",
        position="top-right",
    )


def _label_for(node: dict, *, current: bool) -> str:
    marker = "▶ " if current else ""
    pn = node.get("parent_pass")
    pn_str = f" (forked@P{pn})" if pn else ""
    return (
        f"{marker}{node['id']}  ·  {node.get('task_type') or '—'}{pn_str}  ·  "
        f"+{node.get('improvement') if node.get('improvement') is not None else '—'}%"
    )


def _load_lineage(db_path: Path, run_id: str) -> list[dict]:
    """Pull the run, its ancestor chain, and its direct children."""
    with connect(db_path) as conn:
        # Walk ancestors
        chain: list[dict] = []
        current_id: str | None = run_id
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            row = conn.execute(
                "SELECT r.id, r.parent_run_id, r.parent_pass, r.task_type, "
                "s.improvement FROM runs r LEFT JOIN scores s "
                "ON s.run_id = r.id WHERE r.id = ?",
                (current_id,),
            ).fetchone()
            if not row:
                break
            chain.append(dict(row))
            current_id = row["parent_run_id"]

        # Direct children of the current run
        children = [
            dict(r) for r in conn.execute(
                "SELECT r.id, r.parent_run_id, r.parent_pass, r.task_type, "
                "s.improvement FROM runs r LEFT JOIN scores s "
                "ON s.run_id = r.id WHERE r.parent_run_id = ?",
                (run_id,),
            ).fetchall()
        ]
    # Reverse chain so root → ... → current is preserved
    chain.reverse()
    return chain + [c for c in children if c["id"] not in seen]
