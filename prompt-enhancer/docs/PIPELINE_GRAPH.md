# Pipeline graph configuration (`pipeline.toml`)

The pipeline graph loader at `src/enhancer/core/pipeline_graph.py`
parses a TOML description of the multi-pass pipeline and rejects, at
load time, any configuration that would violate the three frozen
concurrency invariants.

This document is the user-facing reference. The schema is **version 1**.
Until v2.0 wires the loader into `pipeline.py`, the only effect of
authoring a `pipeline.toml` is to round-trip cleanly through the
validator -- the runtime still uses the canonical 4-pass graph encoded
by `pipeline_graph.default_graph()`.

---

## The three load-time invariants

These mirror the runtime invariants enforced by
`tests/test_concurrency.py`. Read `docs/EXTRACTION_GOTCHAS.md` for
context.

1. **Pass 1 -> Pass 2 strictly serial.** The `weakness_detection` pass
   MUST list the `intent_analysis` pass in its `requires`, AND the two
   passes MUST NOT appear in each other's `parallel_with` lists.
2. **Pass 4 awaited before Magnitude / SoT.** Any `magnitude` or `sot`
   pass MUST list a `score` pass in its `requires`.
3. **`idle_timeout = 120` on every streaming pass.** A pass with
   `streams = true` may not configure a different `idle_timeout`.

The validator is `pipeline_graph.validate(graph)` and is exercised by
`tests/test_pipeline_graph.py`. Each violation raises
`PipelineGraphValidationError` (a `ValueError` subclass) with a message
naming the offending node id and the invariant number.

---

## Schema

```toml
version = 1

[[passes]]
id          = "<unique string>"
kind        = "<one of VALID_KINDS>"
requires    = ["<id>", ...]    # default: []
parallel_with = ["<id>", ...]  # default: []
streams     = true | false      # default: false
idle_timeout = 120              # default: 120; rejected if != 120 on streams=true
```

### Pass `kind` values

| kind                  | role in the canonical pipeline                |
| --------------------- | --------------------------------------------- |
| `intent_analysis`     | Pass 1 -- task type detection                 |
| `weakness_detection`  | Pass 2 -- weakness audit + technique pick     |
| `rewrite`             | Pass 3 -- task-aware rewrite (streams)        |
| `score`               | Pass 4 -- quality scoring (background task)   |
| `magnitude`           | post-Pass 4 transform (streams)               |
| `sot`                 | Skeleton-of-Thought transform (streams)       |
| `pretrial`            | one-shot model recommendation                 |

Unknown kinds are rejected (failure mode (e)).

---

## Minimum legal config

The smallest legal `pipeline.toml` that mimics today's 4-pass behavior
without the optional Magnitude/SoT transforms:

```toml
version = 1

[[passes]]
id      = "pass1"
kind    = "intent_analysis"
streams = true

[[passes]]
id       = "pass2_weakness"
kind     = "weakness_detection"
requires = ["pass1"]
streams  = true

[[passes]]
id       = "pass3_rewrite"
kind     = "rewrite"
requires = ["pass2_weakness"]
streams  = true

[[passes]]
id       = "pass4_score"
kind     = "score"
requires = ["pass3_rewrite"]
streams  = true
```

---

## Full canonical config (matches `default_graph()`)

```toml
version = 1

[[passes]]
id      = "pass1"
kind    = "intent_analysis"
streams = true

[[passes]]
id       = "pass2_weakness"
kind     = "weakness_detection"
requires = ["pass1"]   # SERIAL after pass1 -- invariant 1
streams  = true

[[passes]]
id       = "pass3_rewrite"
kind     = "rewrite"
requires = ["pass2_weakness"]
streams  = true

[[passes]]
id       = "pass4_score"
kind     = "score"
requires = ["pass3_rewrite"]
streams  = true

[[passes]]
id       = "magnitude"
kind     = "magnitude"
requires = ["pass4_score"]   # AWAIT pass4 -- invariant 2
streams  = true

[[passes]]
id            = "sot"
kind          = "sot"
requires      = ["pass4_score"]   # AWAIT pass4 -- invariant 2
parallel_with = ["magnitude"]     # may run concurrently with magnitude
streams       = true
```

---

## Validator failure modes

The validator names the offending node id(s) and the invariant number
in each error message. The keywords below are what the test suite
asserts against.

### (a) Invariant 1 -- intent + weakness not serial

Either listing them as `parallel_with` of each other, or omitting the
`requires` edge:

```toml
[[passes]]
id      = "pass1"
kind    = "intent_analysis"
streams = true

[[passes]]
id      = "pass2"
kind    = "weakness_detection"
streams = true
# BAD -- no requires = ["pass1"]
```

Error keyword: **`invariant 1`**.

### (b) Invariant 2 -- magnitude/sot skips score

```toml
[[passes]]
id       = "mag"
kind     = "magnitude"
requires = ["pass3_rewrite"]   # BAD -- skips Pass 4
streams  = true
```

Error keyword: **`invariant 2`**.

### (c) Invariant 3 -- streaming idle_timeout != 120

```toml
[[passes]]
id           = "pass1"
kind         = "intent_analysis"
streams      = true
idle_timeout = 60   # BAD
```

Error keyword: **`invariant 3`** (and the message includes
`idle_timeout`).

### (d) Cycle in the requires graph

```toml
[[passes]]
id       = "a"
kind     = "intent_analysis"
requires = ["b"]
streams  = true

[[passes]]
id       = "b"
kind     = "weakness_detection"
requires = ["a"]   # BAD -- mutual dependency
streams  = true
```

Error keyword: **`cycle`**.

### (e) Unknown kind

```toml
[[passes]]
id   = "weird"
kind = "interpretive_dance"   # BAD
```

Error keyword: **`unknown kind`**.

### (f) Duplicate id

```toml
[[passes]]
id   = "pass1"
kind = "intent_analysis"

[[passes]]
id   = "pass1"   # BAD -- collision
kind = "weakness_detection"
```

Error keyword: **`duplicate id`**.

### Other shape checks (graceful failures)

* Missing `id` or `kind`: caught with a `PipelineGraphValidationError`
  naming the offending entry index -- never bare `KeyError`.
* `requires` / `parallel_with` referencing unknown ids:
  **`unknown id`**.
* `version = 2` (anything other than 1):
  **`unsupported schema version`**.
* Self-references in `requires` or `parallel_with`: rejected.
* Empty `passes` array: rejected.

---

## Round-trip

`pipeline_graph.to_toml_dict(graph)` plus `tomli_w.dumps` produces TOML
that loads back to an equal `PipelineGraph`. The test suite covers
this round-trip for the default graph including `parallel_with` edges.

---

## Default behavior

`pipeline_graph.load(None)` and `load("/path/that/does/not/exist")`
both return `default_graph()`. This is the v2.0 contract: a missing
`pipeline.toml` means "use the canonical pipeline". Only an explicit,
on-disk file activates the loader's reject path.
