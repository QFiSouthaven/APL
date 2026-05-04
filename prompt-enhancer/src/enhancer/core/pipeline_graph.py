"""TOML-driven pipeline graph configuration loader + STATIC validator.

This module is the load-time guard for v2.0's configurable pipeline. It
parses a ``pipeline.toml`` description of the multi-pass pipeline into a
:class:`PipelineGraph` and rejects any configuration that would violate
the three frozen concurrency invariants documented in
``docs/EXTRACTION_GOTCHAS.md`` and at the top of
``src/enhancer/core/pipeline.py``:

1. **Pass 1 -> Pass 2 STRICTLY SERIAL.** ``intent_analysis`` and
   ``weakness_detection`` may NOT appear in each other's
   ``parallel_with`` list. Equivalently, the weakness pass MUST list the
   intent pass in its ``requires``.
2. **Pass 4 awaited BEFORE Magnitude/SoT.** Any ``magnitude`` or ``sot``
   pass MUST list a ``score`` pass in its ``requires``.
3. **idle_timeout=120 on every stream.** Streaming passes
   (``streams=true``) may not configure a different ``idle_timeout``.

This module does NOT touch :mod:`enhancer.core.pipeline`; wiring is a
follow-up task. The validator can be exercised standalone.

Schema (version 1)
------------------

.. code-block:: toml

    version = 1

    [[passes]]
    id          = "pass1"
    kind        = "intent_analysis"
    streams     = true
    # idle_timeout (optional) defaults to 120 on streaming passes;
    # it MUST equal 120 if specified -- invariant 3.

    [[passes]]
    id          = "pass2_weakness"
    kind        = "weakness_detection"
    requires    = ["pass1"]   # serial after pass1 -- invariant 1
    streams     = true

    [[passes]]
    id          = "pass3_rewrite"
    kind        = "rewrite"
    requires    = ["pass2_weakness"]
    streams     = true

    [[passes]]
    id          = "pass4_score"
    kind        = "score"
    requires    = ["pass3_rewrite"]
    streams     = true

    [[passes]]
    id            = "magnitude"
    kind          = "magnitude"
    requires      = ["pass4_score"]   # await pass4 -- invariant 2
    streams       = true

    [[passes]]
    id            = "sot"
    kind          = "sot"
    requires      = ["pass4_score"]
    parallel_with = ["magnitude"]
    streams       = true

The default in-code graph (returned by :func:`default_graph`) matches
the canonical 4-pass pipeline currently encoded in ``pipeline.py`` and
is what :func:`load` returns when the user's ``pipeline.toml`` is
absent.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover -- py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# ─── public types ────────────────────────────────────────────────────────


SCHEMA_VERSION = 1
"""TOML schema version this loader speaks."""


VALID_KINDS: frozenset[str] = frozenset(
    {
        "intent_analysis",       # Pass 1
        "weakness_detection",    # Pass 2
        "rewrite",               # Pass 3
        "score",                 # Pass 4
        "magnitude",             # post-Pass 4 transform
        "sot",                   # post-Pass 4 transform
        "pretrial",              # one-shot model recommendation
    }
)
"""Allowed values for :attr:`PassNode.kind`."""


REQUIRED_STREAM_IDLE_TIMEOUT = 120
"""Invariant 3: every chat_stream call uses idle_timeout=120."""


class PipelineGraphValidationError(ValueError):
    """Raised when a TOML config violates schema or invariants.

    Subclasses :class:`ValueError` so callers may catch either. The
    message always contains a concrete diagnostic and, where applicable,
    the invariant number that was violated (e.g. ``"invariant 1"``).
    """


@dataclass(frozen=True)
class PassNode:
    """One pipeline pass in the graph.

    Attributes
    ----------
    id:
        Unique identifier for this node within the graph (e.g.
        ``"pass1"``, ``"pass2_weakness"``). Referenced by other nodes'
        ``requires`` and ``parallel_with`` lists.
    kind:
        What this pass does. Must be one of :data:`VALID_KINDS`.
    requires:
        IDs of passes that must complete BEFORE this one runs (serial
        dependencies). The graph induced by ``requires`` must be acyclic.
    parallel_with:
        IDs of passes that MAY run concurrently with this one once their
        ``requires`` are satisfied. Symmetric in spirit but not enforced
        as such -- the validator simply forbids invariant-violating
        pairs (e.g. intent + weakness).
    streams:
        ``True`` if this pass uses ``provider.chat_stream``; subject to
        invariant 3 (idle_timeout=120).
    idle_timeout:
        Honoured only when ``streams=True``. Defaults to
        :data:`REQUIRED_STREAM_IDLE_TIMEOUT`. The validator rejects any
        other value on a streaming pass.
    """

    id: str
    kind: str
    requires: tuple[str, ...] = ()
    parallel_with: tuple[str, ...] = ()
    streams: bool = False
    idle_timeout: int = REQUIRED_STREAM_IDLE_TIMEOUT


@dataclass(frozen=True)
class PipelineGraph:
    """A parsed + validated pipeline configuration."""

    nodes: tuple[PassNode, ...]
    version: int = SCHEMA_VERSION

    def by_id(self, node_id: str) -> PassNode | None:
        """Return the node with ``node_id`` or ``None`` if absent."""
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def of_kind(self, kind: str) -> tuple[PassNode, ...]:
        """All nodes whose ``kind`` matches."""
        return tuple(n for n in self.nodes if n.kind == kind)


# ─── canonical default graph ─────────────────────────────────────────────


def default_graph() -> PipelineGraph:
    """The canonical 4-pass graph encoded in code.

    Matches today's :func:`enhancer.core.pipeline.run_pipeline` behavior
    one-for-one. This IS what :func:`load` returns when the user has no
    ``pipeline.toml`` on disk.
    """
    nodes: tuple[PassNode, ...] = (
        PassNode(
            id="pass1",
            kind="intent_analysis",
            streams=True,
        ),
        PassNode(
            id="pass2_weakness",
            kind="weakness_detection",
            requires=("pass1",),
            streams=True,
        ),
        PassNode(
            id="pass3_rewrite",
            kind="rewrite",
            requires=("pass2_weakness",),
            streams=True,
        ),
        PassNode(
            id="pass4_score",
            kind="score",
            requires=("pass3_rewrite",),
            streams=True,
        ),
        PassNode(
            id="magnitude",
            kind="magnitude",
            requires=("pass4_score",),
            parallel_with=("sot",),
            streams=True,
        ),
        PassNode(
            id="sot",
            kind="sot",
            requires=("pass4_score",),
            parallel_with=("magnitude",),
            streams=True,
        ),
    )
    g = PipelineGraph(nodes=nodes, version=SCHEMA_VERSION)
    # Sanity: keeps default_graph() honest as an invariant regression test.
    validate(g)
    return g


# ─── load + serialize ────────────────────────────────────────────────────


def load(path: Path | str | None) -> PipelineGraph:
    """Read ``path`` (a ``pipeline.toml``), validate, return the graph.

    If ``path`` is ``None`` or does not exist, returns
    :func:`default_graph` -- this is the v2.0 contract: a missing config
    file means "use the canonical pipeline."

    Raises
    ------
    PipelineGraphValidationError
        On any schema or invariant violation. The message names the
        offending node id(s) and the invariant number when applicable.
    """
    if path is None:
        return default_graph()

    p = Path(path)
    if not p.exists():
        return default_graph()

    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PipelineGraphValidationError(
            f"could not parse {p}: {exc}"
        ) from exc

    return _from_dict(data)


def _from_dict(data: dict[str, Any]) -> PipelineGraph:
    """Build + validate a PipelineGraph from a parsed-TOML dict."""
    if not isinstance(data, dict):
        raise PipelineGraphValidationError(
            "top-level TOML must be a table"
        )

    version = data.get("version", SCHEMA_VERSION)
    if not isinstance(version, int):
        raise PipelineGraphValidationError(
            f"version must be an integer, got {type(version).__name__}"
        )
    if version != SCHEMA_VERSION:
        raise PipelineGraphValidationError(
            f"unsupported schema version {version}; "
            f"this loader speaks version {SCHEMA_VERSION}"
        )

    raw_passes = data.get("passes")
    if raw_passes is None:
        raise PipelineGraphValidationError(
            "config must contain at least one [[passes]] entry"
        )
    if not isinstance(raw_passes, list):
        raise PipelineGraphValidationError(
            "'passes' must be an array of tables ([[passes]])"
        )
    if not raw_passes:
        raise PipelineGraphValidationError(
            "config must contain at least one [[passes]] entry"
        )

    nodes: list[PassNode] = []
    for i, entry in enumerate(raw_passes):
        if not isinstance(entry, dict):
            raise PipelineGraphValidationError(
                f"[[passes]] entry #{i} must be a table"
            )
        nodes.append(_pass_from_dict(entry, index=i))

    graph = PipelineGraph(nodes=tuple(nodes), version=version)
    validate(graph)
    return graph


def _pass_from_dict(entry: dict[str, Any], *, index: int) -> PassNode:
    """Coerce one [[passes]] dict into a PassNode (no semantic validation)."""
    # Required keys.
    if "id" not in entry:
        raise PipelineGraphValidationError(
            f"[[passes]] entry #{index} missing required 'id'"
        )
    if "kind" not in entry:
        raise PipelineGraphValidationError(
            f"[[passes]] entry #{index} (id={entry.get('id')!r}) "
            "missing required 'kind'"
        )

    node_id = entry["id"]
    if not isinstance(node_id, str) or not node_id:
        raise PipelineGraphValidationError(
            f"[[passes]] entry #{index} 'id' must be a non-empty string"
        )

    kind = entry["kind"]
    if not isinstance(kind, str) or not kind:
        raise PipelineGraphValidationError(
            f"pass {node_id!r}: 'kind' must be a non-empty string"
        )

    requires = entry.get("requires", ())
    if not isinstance(requires, (list, tuple)) or not all(
        isinstance(x, str) for x in requires
    ):
        raise PipelineGraphValidationError(
            f"pass {node_id!r}: 'requires' must be a list of strings"
        )

    parallel_with = entry.get("parallel_with", ())
    if not isinstance(parallel_with, (list, tuple)) or not all(
        isinstance(x, str) for x in parallel_with
    ):
        raise PipelineGraphValidationError(
            f"pass {node_id!r}: 'parallel_with' must be a list of strings"
        )

    streams = entry.get("streams", False)
    if not isinstance(streams, bool):
        raise PipelineGraphValidationError(
            f"pass {node_id!r}: 'streams' must be a boolean"
        )

    idle_timeout = entry.get("idle_timeout", REQUIRED_STREAM_IDLE_TIMEOUT)
    if not isinstance(idle_timeout, int) or isinstance(idle_timeout, bool):
        raise PipelineGraphValidationError(
            f"pass {node_id!r}: 'idle_timeout' must be an integer"
        )

    return PassNode(
        id=node_id,
        kind=kind,
        requires=tuple(requires),
        parallel_with=tuple(parallel_with),
        streams=streams,
        idle_timeout=idle_timeout,
    )


def to_toml_dict(graph: PipelineGraph) -> dict[str, Any]:
    """Serialize a graph back to a TOML-ready dict (round-trip helper)."""
    passes: list[dict[str, Any]] = []
    for n in graph.nodes:
        d: dict[str, Any] = {"id": n.id, "kind": n.kind}
        if n.requires:
            d["requires"] = list(n.requires)
        if n.parallel_with:
            d["parallel_with"] = list(n.parallel_with)
        d["streams"] = n.streams
        d["idle_timeout"] = n.idle_timeout
        passes.append(d)
    return {"version": graph.version, "passes": passes}


# ─── pure validator ─────────────────────────────────────────────────────


def validate(graph: PipelineGraph) -> None:
    """Pure validator -- raises :class:`PipelineGraphValidationError` on
    any violation; returns ``None`` when the graph is legal.

    Failure modes
    -------------
    a. **invariant 1**: ``intent_analysis`` and ``weakness_detection``
       in each other's ``parallel_with`` lists -- OR -- the weakness
       pass does not list the intent pass in its ``requires``.
    b. **invariant 2**: a ``magnitude`` or ``sot`` pass does not list a
       ``score`` pass in its ``requires``.
    c. **invariant 3**: a ``streams=true`` pass has
       ``idle_timeout != 120``.
    d. cycle in the ``requires`` graph.
    e. unknown ``kind`` value.
    f. duplicate ``id``.

    Plus a few schema-shape checks: dangling references in
    ``requires``/``parallel_with``, self-references.
    """
    nodes = graph.nodes

    # (f) Duplicate ids -- check first; the rest assume unique ids.
    seen: set[str] = set()
    for n in nodes:
        if n.id in seen:
            raise PipelineGraphValidationError(
                f"duplicate id {n.id!r}"
            )
        seen.add(n.id)

    # (e) Unknown kinds.
    for n in nodes:
        if n.kind not in VALID_KINDS:
            raise PipelineGraphValidationError(
                f"pass {n.id!r}: unknown kind {n.kind!r}; "
                f"must be one of {sorted(VALID_KINDS)}"
            )

    # Dangling/self references in requires + parallel_with.
    for n in nodes:
        for ref in n.requires:
            if ref == n.id:
                raise PipelineGraphValidationError(
                    f"pass {n.id!r}: cannot require itself"
                )
            if ref not in seen:
                raise PipelineGraphValidationError(
                    f"pass {n.id!r}: 'requires' references unknown id {ref!r}"
                )
        for ref in n.parallel_with:
            if ref == n.id:
                raise PipelineGraphValidationError(
                    f"pass {n.id!r}: cannot run parallel with itself"
                )
            if ref not in seen:
                raise PipelineGraphValidationError(
                    f"pass {n.id!r}: 'parallel_with' references "
                    f"unknown id {ref!r}"
                )

    # (c) Invariant 3: streaming passes must use idle_timeout=120.
    for n in nodes:
        if n.streams and n.idle_timeout != REQUIRED_STREAM_IDLE_TIMEOUT:
            raise PipelineGraphValidationError(
                f"pass {n.id!r}: idle_timeout must be "
                f"{REQUIRED_STREAM_IDLE_TIMEOUT} on streaming passes "
                f"(invariant 3); got {n.idle_timeout}"
            )

    # (a) Invariant 1: intent_analysis + weakness_detection must be SERIAL.
    intents = graph.of_kind("intent_analysis")
    weaknesses = graph.of_kind("weakness_detection")
    for w in weaknesses:
        for i in intents:
            # Direct violation: each in the other's parallel_with.
            if i.id in w.parallel_with or w.id in i.parallel_with:
                raise PipelineGraphValidationError(
                    f"pass {w.id!r} (weakness_detection) and "
                    f"{i.id!r} (intent_analysis) declared parallel; "
                    "they must run STRICTLY SERIAL (invariant 1)"
                )
            # Inverse rule: weakness must require intent (the load-bearing
            # serial dep). With multiple intents, AT LEAST ONE must be in
            # the weakness pass's requires (transitively, but a static
            # check approximates that via direct edges in the simple case).
        if intents and not any(i.id in w.requires for i in intents):
            ids = ", ".join(repr(i.id) for i in intents)
            raise PipelineGraphValidationError(
                f"pass {w.id!r} (weakness_detection) must list an "
                f"intent_analysis pass ({ids}) in its 'requires' to "
                "enforce strictly-serial execution (invariant 1)"
            )

    # (b) Invariant 2: magnitude/sot must require a score pass.
    score_ids = {n.id for n in graph.of_kind("score")}
    for n in nodes:
        if n.kind in {"magnitude", "sot"}:
            if not score_ids:
                raise PipelineGraphValidationError(
                    f"pass {n.id!r} (kind={n.kind!r}) requires a "
                    "'score' pass to exist and be listed in 'requires' "
                    "(invariant 2: Pass 4 awaited before Magnitude/SoT)"
                )
            if not any(r in score_ids for r in n.requires):
                raise PipelineGraphValidationError(
                    f"pass {n.id!r} (kind={n.kind!r}) must list a "
                    "score pass in its 'requires' so Pass 4 is awaited "
                    "before this one streams (invariant 2)"
                )

    # (d) Cycle detection (Kahn's algorithm on the requires DAG).
    in_degree = {n.id: 0 for n in nodes}
    edges: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for r in n.requires:
            edges[r].append(n.id)
            in_degree[n.id] += 1
    queue = [nid for nid, d in in_degree.items() if d == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for succ in edges[cur]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)
    if visited != len(nodes):
        offending = [nid for nid, d in in_degree.items() if d > 0]
        raise PipelineGraphValidationError(
            f"cycle detected in 'requires' graph involving: "
            f"{sorted(offending)}"
        )


# ─── public surface ─────────────────────────────────────────────────────

__all__ = [
    "PassNode",
    "PipelineGraph",
    "PipelineGraphValidationError",
    "SCHEMA_VERSION",
    "VALID_KINDS",
    "REQUIRED_STREAM_IDLE_TIMEOUT",
    "default_graph",
    "load",
    "to_toml_dict",
    "validate",
]
