"""Static validator + TOML round-trip tests for the pipeline graph loader.

The validator is the load-time guard for v2.0's configurable pipeline.
These tests exercise:

* :func:`default_graph` returns the canonical 4-pass graph and passes
  :func:`validate` (sanity).
* The example TOML in ``docs/PIPELINE_GRAPH.md`` parses back to the
  default graph (modulo IDs).
* Each of the 6 documented validator failure modes (a-f) raises with a
  diagnostic message containing the expected keyword.
* ``version=2`` raises with ``"unsupported schema version"``.
* Malformed structure (missing ``id``, missing ``kind``, wrong types)
  raises gracefully with a helpful message -- never bare ``KeyError``.
* Round-trip via :func:`to_toml_dict` + ``tomli_w`` + :func:`load` is
  identity.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import tomli_w

from enhancer.core import pipeline_graph as pg

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover -- py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# ─── helpers ────────────────────────────────────────────────────────────


def _write_toml(path: Path, data: dict) -> Path:
    path.write_bytes(tomli_w.dumps(data).encode("utf-8"))
    return path


# Example TOML block from docs/PIPELINE_GRAPH.md and the task brief.
EXAMPLE_TOML_DICT: dict = {
    "version": 1,
    "passes": [
        {"id": "pass1", "kind": "intent_analysis", "streams": True},
        {
            "id": "pass2_weakness",
            "kind": "weakness_detection",
            "requires": ["pass1"],
            "streams": True,
        },
        {
            "id": "pass3_rewrite",
            "kind": "rewrite",
            "requires": ["pass2_weakness"],
            "streams": True,
        },
        {
            "id": "pass4_score",
            "kind": "score",
            "requires": ["pass3_rewrite"],
            "streams": True,
        },
        {
            "id": "magnitude",
            "kind": "magnitude",
            "requires": ["pass4_score"],
            "streams": True,
        },
        {
            "id": "sot",
            "kind": "sot",
            "requires": ["pass4_score"],
            "parallel_with": ["magnitude"],
            "streams": True,
        },
    ],
}


# ─── default graph ──────────────────────────────────────────────────────


def test_default_graph_has_canonical_4_passes_plus_transforms() -> None:
    g = pg.default_graph()
    kinds = [n.kind for n in g.nodes]
    assert kinds == [
        "intent_analysis",
        "weakness_detection",
        "rewrite",
        "score",
        "magnitude",
        "sot",
    ]


def test_default_graph_passes_validate() -> None:
    # Sanity: default_graph() should never violate its own invariants.
    pg.validate(pg.default_graph())  # must not raise


def test_default_graph_pass1_pass2_are_serial() -> None:
    g = pg.default_graph()
    p1 = g.of_kind("intent_analysis")[0]
    p2 = g.of_kind("weakness_detection")[0]
    assert p1.id in p2.requires, "pass2 must require pass1 (invariant 1)"
    assert p2.id not in p1.parallel_with
    assert p1.id not in p2.parallel_with


def test_default_graph_score_required_by_magnitude_and_sot() -> None:
    g = pg.default_graph()
    p4 = g.of_kind("score")[0]
    for n in (*g.of_kind("magnitude"), *g.of_kind("sot")):
        assert p4.id in n.requires, (
            f"{n.id} must require score pass (invariant 2)"
        )


def test_default_graph_all_streaming_passes_use_120s_idle_timeout() -> None:
    for n in pg.default_graph().nodes:
        if n.streams:
            assert n.idle_timeout == 120


# ─── example TOML matches default graph ─────────────────────────────────


def test_example_toml_parses_to_same_kinds_and_edges_as_default(
    tmp_path: Path,
) -> None:
    cfg = _write_toml(tmp_path / "pipeline.toml", EXAMPLE_TOML_DICT)
    parsed = pg.load(cfg)
    default = pg.default_graph()

    # Same kinds in same order.
    assert [n.kind for n in parsed.nodes] == [n.kind for n in default.nodes]
    # Same dependency structure (by-kind, since IDs are user-chosen).
    parsed_by_kind = {n.kind: n for n in parsed.nodes}
    default_by_kind = {n.kind: n for n in default.nodes}
    assert (
        default_by_kind["intent_analysis"].id
        in {parsed_by_kind["intent_analysis"].id}
    )
    # weakness requires the intent pass, in both graphs.
    p_intent = parsed_by_kind["intent_analysis"].id
    assert p_intent in parsed_by_kind["weakness_detection"].requires


def test_load_with_none_path_returns_default() -> None:
    assert pg.load(None) == pg.default_graph()


def test_load_with_missing_path_returns_default(tmp_path: Path) -> None:
    assert pg.load(tmp_path / "absent.toml") == pg.default_graph()


# ─── failure mode (a): invariant 1 ──────────────────────────────────────


def test_violates_invariant_1_via_parallel_with(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {
                "id": "pass1",
                "kind": "intent_analysis",
                "streams": True,
                "parallel_with": ["pass2"],
            },
            {
                "id": "pass2",
                "kind": "weakness_detection",
                "streams": True,
                "requires": ["pass1"],
                "parallel_with": ["pass1"],
            },
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["pass2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "invariant 1" in str(exc.value)


def test_violates_invariant_1_via_missing_requires_edge(tmp_path: Path) -> None:
    # weakness pass has empty requires -> doesn't depend on intent at all.
    bad = {
        "version": 1,
        "passes": [
            {"id": "pass1", "kind": "intent_analysis", "streams": True},
            {
                "id": "pass2",
                "kind": "weakness_detection",
                "streams": True,
            },  # no requires!
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["pass2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "invariant 1" in str(exc.value)


# ─── failure mode (b): invariant 2 ──────────────────────────────────────


def test_violates_invariant_2_magnitude_skips_score(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
            {"id": "p2", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["p2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
            {
                "id": "mag",
                "kind": "magnitude",
                "streams": True,
                "requires": ["p3"],   # WRONG -- skips p4
            },
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "invariant 2" in str(exc.value)


def test_violates_invariant_2_sot_skips_score(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
            {"id": "p2", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["p2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
            {
                "id": "sot",
                "kind": "sot",
                "streams": True,
                "requires": ["p3"],   # WRONG
            },
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "invariant 2" in str(exc.value)


# ─── failure mode (c): invariant 3 ──────────────────────────────────────


def test_violates_invariant_3_idle_timeout(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {
                "id": "p1",
                "kind": "intent_analysis",
                "streams": True,
                "idle_timeout": 60,   # WRONG
            },
            {"id": "p2", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["p2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    msg = str(exc.value)
    assert "invariant 3" in msg
    assert "idle_timeout" in msg


def test_invariant_3_does_not_apply_to_non_streaming_passes(
    tmp_path: Path,
) -> None:
    # A non-streaming pass with an idle_timeout != 120 is fine; it doesn't
    # gate any streaming call.
    cfg_dict = {
        "version": 1,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
            {"id": "p2", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["p2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
            {
                "id": "pre",
                "kind": "pretrial",
                "streams": False,
                "idle_timeout": 30,   # OK -- not streaming
            },
        ],
    }
    cfg = _write_toml(tmp_path / "ok.toml", cfg_dict)
    pg.load(cfg)  # must not raise


# ─── failure mode (d): cycle ────────────────────────────────────────────


def test_cycle_in_requires_graph_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "a", "kind": "intent_analysis", "streams": True,
             "requires": ["b"]},
            {"id": "b", "kind": "weakness_detection", "streams": True,
             "requires": ["a"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["b"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "cycle" in str(exc.value)


# ─── failure mode (e): unknown kind ─────────────────────────────────────


def test_unknown_kind_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
            {"id": "weird", "kind": "interpretive_dance", "streams": True,
             "requires": ["p1"]},
            {"id": "p2", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["p2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "unknown kind" in str(exc.value)


# ─── failure mode (f): duplicate id ─────────────────────────────────────


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
            {"id": "p1", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "duplicate id" in str(exc.value)


# ─── version handling ──────────────────────────────────────────────────


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 2,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "unsupported schema version" in str(exc.value)


def test_version_omitted_defaults_to_1(tmp_path: Path) -> None:
    # Drop the version key entirely; loader should treat it as v1.
    cfg_dict = {
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True},
            {"id": "p2", "kind": "weakness_detection", "streams": True,
             "requires": ["p1"]},
            {"id": "p3", "kind": "rewrite", "streams": True,
             "requires": ["p2"]},
            {"id": "p4", "kind": "score", "streams": True,
             "requires": ["p3"]},
        ],
    }
    cfg = _write_toml(tmp_path / "ok.toml", cfg_dict)
    g = pg.load(cfg)
    assert g.version == 1


# ─── malformed structure ────────────────────────────────────────────────


def test_missing_id_raises_validation_error_not_keyerror(
    tmp_path: Path,
) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"kind": "intent_analysis", "streams": True},  # no id
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "id" in str(exc.value).lower()


def test_missing_kind_raises_validation_error_not_keyerror(
    tmp_path: Path,
) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "p1", "streams": True},  # no kind
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "kind" in str(exc.value).lower()


def test_passes_must_be_array(tmp_path: Path) -> None:
    bad = {"version": 1, "passes": "not an array"}
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError):
        pg.load(cfg)


def test_empty_passes_array_rejected(tmp_path: Path) -> None:
    bad = {"version": 1, "passes": []}
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError):
        pg.load(cfg)


def test_dangling_requires_reference_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "passes": [
            {"id": "p1", "kind": "intent_analysis", "streams": True,
             "requires": ["does_not_exist"]},
        ],
    }
    cfg = _write_toml(tmp_path / "bad.toml", bad)
    with pytest.raises(pg.PipelineGraphValidationError) as exc:
        pg.load(cfg)
    assert "unknown id" in str(exc.value)


def test_corrupted_toml_raises_validation_error(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("this is [not [valid \x00 toml ::: \n[[[", encoding="utf-8")
    with pytest.raises(pg.PipelineGraphValidationError):
        pg.load(cfg)


# ─── round-trip ────────────────────────────────────────────────────────


def test_round_trip_default_graph(tmp_path: Path) -> None:
    g = pg.default_graph()
    cfg = tmp_path / "pipeline.toml"
    cfg.write_bytes(tomli_w.dumps(pg.to_toml_dict(g)).encode("utf-8"))
    reloaded = pg.load(cfg)
    assert reloaded == g


def test_round_trip_preserves_parallel_with(tmp_path: Path) -> None:
    g = pg.default_graph()
    # The default graph has magnitude/sot in each other's parallel_with.
    cfg = tmp_path / "pipeline.toml"
    cfg.write_bytes(tomli_w.dumps(pg.to_toml_dict(g)).encode("utf-8"))
    reloaded = pg.load(cfg)
    mag = reloaded.by_id("magnitude")
    sot = reloaded.by_id("sot")
    assert mag is not None and sot is not None
    assert "sot" in mag.parallel_with
    assert "magnitude" in sot.parallel_with
