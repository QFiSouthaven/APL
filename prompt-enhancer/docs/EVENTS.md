# Event contract

The 30-member `EventType` enum at `enhancer/core/events.py` is the
standalone's API boundary. Any consumer (CLI, NiceGUI, REST adapter,
the monolith's `devflow.py`, `chain_events.py`) reads against these
names.

**Renaming or repurposing an existing event is a v2 migration.** Adding
a new event is fine.

> A canonical reference also lives at
> `~/.claude/knowledge/prompt-enhancer-event-contract.md` for cross-
> session continuity.

---

## All 30 events

```
agent_step
agent_pass_start
agent_pass_chunk
agent_pass_result
agent_pipeline_summary
enhancement_score
agent_done
agent_error

agent_disambiguate

persona_start
persona_result

magnitude_start
magnitude_chunk
magnitude_done
magnitude_error

sot_start
sot_chunk
sot_done
sot_error

pretrial_start
pretrial_result
pretrial_error

session_created
session_list
session_loaded
session_renamed
session_cleared
session_deleted
session_entry_added
session_active
```

## Public-contract flags

| Flag | Set when | Read by |
|---|---|---|
| `scores_fallback: true` | Pass 4 errored OR returned empty OR was skipped because Pass 3 fell back to original | UI shows "scoring skipped"; analytics excludes from average |
| `pass3_partial: true` | Pass 3 stream errored mid-flow OR yielded zero chunks → fell back to original prompt | UI shows warning; pipeline skips self-correction retry |

## Payload schemas

### `agent_pass_start`

```python
{ "pass_number": int, "pass_name": str, "model": str }
```

### `agent_pass_chunk` (streamed)

```python
{ "pass_number": int, "token": str }
```

### `agent_pass_result`

```python
{
    "pass_number": int,
    "pass_name": str,
    "content": str,
    "model": str,
    "duration_ms": int,
    "task_type": str | None,    # Pass 1 only
    "technique": str | None,    # Pass 2 only
    "scores": dict | None,      # Pass 4 only
}
```

### `agent_disambiguate`

```python
{
    "disambig_id": str,
    "questions": [
        {"question": str, "options": [str, str, str]},
        ...
    ],
}
```

See `prompt-enhancer-disambiguation.md` (knowledge folder) and
`docs/EXTRACTION_GOTCHAS.md` for the resume protocol.

### `enhancement_score`

```python
{
    "scores": {"specificity": int, "constraints": int,
               "actionability": int, "improvement": int},
    "scores_fallback": bool,
    "pass_times_ms": dict,
    "scorer_model": str,
}
```

### `agent_done` (terminal)

```python
{
    "result": str,
    "technique": str,    # canonical: precision | context | structure
    "task_type": str,    # canonical: creative | analytical | factual |
                         #            instructional | conversational | coding
    "scores": dict,
    "scores_fallback": bool,
    "run_id": str,
}
```

### `agent_error`

```python
{ "step": str, "error": str }
```

`step` ∈ `{pass1, pass2, pass3, pass4, disambiguate, session}`. New
phases must add a documented `step` value.

### Magnitude / SoT chunk events

```python
# magnitude_chunk / sot_chunk
{ "token": str }

# magnitude_done / sot_done
{ "content": str }

# magnitude_error / sot_error
{ "error": str }
```

### Pretrial

```python
# pretrial_result
{
    "category": str,            # coding | creative | analytical | ...
    "recommended": str,          # exact model id
    "confidence": str,           # high | medium | low
    "reasoning": str,
    "available_models": [str],
}
```

### Session events

All session events carry the relevant fields from the affected session:
`{ "session_id", "name", "entry_count", "created_at", "updated_at" }`.
`session_list` carries an array; `session_active` may carry
`{"session_id": null}`.

---

## Adding a new event (v0.x)

1. Add an enum member to `EventType` in `core/events.py`.
2. Document the payload above.
3. Emit it from `core/pipeline.py` via
   `await _emit(on_event, EventType.NEW, **payload)`.
4. Update the count in this doc.
5. **Do not** change existing event names or required fields.

## Removing or renaming (v2.0 only)

1. Bump `__version__` in `enhancer/__init__.py` to 2.0.0.
2. Document in `docs/MIGRATION.md`.
3. Ship a compat layer that emits both v1 and v2 names for one
   release.
4. Remove v1 names in 2.1.

---

## OutputContract (for monolith consumers)

The monolith's `chain_events.py` reads against
`swarm-agent-dev/src/webui/contracts.py::MOD_OUTPUTS["agent"]["enhance"]`:

```python
OutputContract(
    completion_event="agent_done",
    output_fields=frozenset({"result", "technique", "task_type",
                             "scores", "scores_fallback"}),
    accumulate_from={
        "scores": "enhancement_score",
        "technique": "agent_pipeline_summary",
        "task_type": "agent_pipeline_summary",
        "persona": "agent_pipeline_summary",
    },
)
```

The standalone's `PipelineResult` carries the same field names so a
future migration adapter (or `chain_events.py` rewrite) can route
identically.
