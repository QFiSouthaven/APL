# Inter-product integration

> The four products in the loop — **Prompt Enhancer**, **Round Robin**,
> **Interpreter**, and (optional) **swarm-loop CLI** — communicate over
> HTTP with a shared JSON envelope. No shared in-process Python
> package is required. Each product remains independently runnable,
> testable, and deployable.

## Loop topology

```
              ┌──────────── 1 user task call ────────────┐
              │                                          │
              ▼                                          │
       ┌─────────────┐                                   │
       │ Interpreter │  (the orchestrator — single       │
       │             │   entry point for the loop)       │
       └─────┬───────┘                                   │
             │                                           │
             │  initial enhancement                      │
             ▼                                           │
     ┌───────────────┐                                   │
     │Prompt Enhancer│  ◄────── re-enhance per cycle ────┤
     │  POST /api/   │  (optional — Interpreter may also │
     │   enhance     │   craft the next prompt itself)   │
     └───────┬───────┘                                   │
             │ EnhancedEnvelope                          │
             ▼                                           │
     ┌───────────────┐                                   │
     │  Round Robin  │  (LM Link bridges Alpha + Bravo;  │
     │  POST /api/   │   each agent runs on a different  │
     │  run/start    │   LM Studio host)                 │
     └───────┬───────┘                                   │
             │ TranscriptEnvelope                        │
             ▼                                           │
       ┌─────────────┐                                   │
       │ Interpreter │ ─── synthesize + decide next ─────┘
       │             │
       │ • stop when convergence/budget/manual
       │ • optional MCP tool calls for file I/O
       │ • write next EnhancedEnvelope
       └─────────────┘
```

**Single user task call** kicks off Interpreter's
`POST /api/loop/start`. Interpreter cycles internally until a stop
condition fires. Each cycle persists its envelope chain so the user
can replay the trajectory after the fact.

---

## Modularity constraints

1. **Each product runs solo.** Round Robin works as a standalone
   dialogue tool. Prompt Enhancer works as a standalone enhancer.
   Interpreter only requires PE + RR when actually orchestrating a
   loop.
2. **No shared Python package required.** v1 integration is HTTP +
   JSON only. Optional thin client library is sugar.
3. **Versioned envelopes.** Every payload carries `schema_version`.
4. **Discovery via `services.toml`** — see below.
5. **Each product owns its OpenAPI spec** at `/openapi.json` (FastAPI
   gives this for free).

---

## Shared envelope schemas

### `EnhancedEnvelope` (PE → Interpreter / RR)

What PE returns from `POST /api/enhance`:

```json
{
  "schema_version": "1.0",
  "prompt": "<original prompt>",
  "enhanced_prompt": "<after PE>",
  "task_type": "creative|analytical|factual|instructional|conversational|coding",
  "technique": "precision|context|structure",
  "persona": "Senior Distributed Systems Architect ..." or null,
  "scores": {
    "specificity":   1..10,
    "constraints":   1..10,
    "actionability": 1..10,
    "improvement":   0..100
  },
  "scores_fallback": false,
  "pass3_partial": false,
  "metadata": {
    "model": "gptoss-120b-...",
    "scorer_model": "qwen3.5-9b-...",
    "temperature": 0.7,
    "max_tokens_scale": 1.0,
    "pass_times_ms": {"pass1":..., "pass2":..., "pass3":..., "pass4":...},
    "magnitude_output": "<may be empty>",
    "sot_output": "<may be empty>"
  },
  "provenance": {
    "source": "prompt_enhancer",
    "run_id": "5289124687aaae92",
    "ts": "2026-04-28T12:34:56.789Z",
    "loop_iteration": 0
  },
  "extras": {}
}
```

### `TranscriptEnvelope` (RR → Interpreter)

What Round Robin's session storage exposes via
`GET /api/sessions/<id>`:

```json
{
  "schema_version": "1.0",
  "session_id": "run-2026-04-28T12-34-56",
  "config": {
    "agents": [
      {"name": "Alpha", "model": "gptoss-120b-...", "host": "local"},
      {"name": "Bravo", "model": "qwen3.5-9b-...",  "host": "remote"}
    ],
    "max_turns": 10,
    "intel_collab_directive": true
  },
  "turns": [
    {
      "turn": 1, "agent_name": "Alpha", "model": "...",
      "content": "...", "latency_ms": 12700, "token_count": 287,
      "ts": "2026-04-28T12:35:09Z"
    },
    ...
  ],
  "nudges": [
    {"reason": "redundant", "after_agent": "Alpha", "turn": 5, "content": "..."}
  ],
  "charlie_ops": [
    {"op": "write_file", "path": "src/foo.py", "turn": 7}
  ],
  "status": "done | stopped | error",
  "started_at": "...",
  "ended_at": "...",
  "provenance": {
    "source": "round_robin",
    "session_id": "run-2026-04-28T12-34-56",
    "ts": "...",
    "loop_iteration": 0
  }
}
```

The **canonical source** for this shape is round-robin's own
`data/sessions/run-<ts>.json`. PE doesn't define it — PE just consumes
it via Interpreter. The user's round-robin repo at
`C:\Users\Falki\round-robin\src\round_robin\sessions.py` is authoritative.

### `LoopEnvelope` (Interpreter cycle output)

What Interpreter emits per cycle and at terminal:

```json
{
  "schema_version": "1.0",
  "loop_id": "loop-abc123",
  "iteration": 3,
  "stop_reason": null,        // "converged" | "budget" | "manual" | null while running
  "current_enhanced": { ...EnhancedEnvelope... },
  "previous_transcript": { ...TranscriptEnvelope... },
  "synthesis": "<interpreter's distilled summary>",
  "next_prompt": "<the prompt for the next RR run>",
  "metrics": {
    "improvement_trend": [55, 70, 82],   // per-iteration scores
    "tokens_in_total":  47500,
    "tokens_out_total": 38200,
    "wall_time_s": 624.5
  },
  "provenance": {
    "source": "interpreter",
    "loop_id": "loop-abc123",
    "iteration": 3,
    "ts": "..."
  }
}
```

---

## Per-product HTTP contracts

### Prompt Enhancer (port 8765)

**`POST /api/enhance`** — request body:

```json
{
  "prompt": "<required, min 1 char>",
  "model": "gptoss-120b-..." or null,
  "scorer_model": "..." or null,
  "temperature": 0.7,
  "max_tokens_scale": 1.0,
  "persona_mode": false,
  "magnitude_mode": false,
  "sot_mode": false,
  "skip_clarify": true,
  "session_id": null,
  "loop_iteration": 0
}
```

Returns `EnhancedEnvelope`. Auto-resumes any disambiguation pause when
`skip_clarify=true` (sibling-product default).

**`GET /api/health`** — returns
`{ok, version, default_model, schema_version}`.

**`GET /api/peers`** — returns the `services.toml` view.

**`GET /openapi.json`** — full FastAPI spec (auto-generated).

### Round Robin (port 8766)

**`POST /api/run/start`** — see round-robin's existing contract at
`src/round_robin/server.py`. Body carries `{config: {agents, max_turns,
intel_*, ...}}`. Sibling products should set the seed prompt by way
of the existing config field for the first agent's input (round-robin
doc: "the first turn is seeded by the user message field").

**`GET /api/sessions/<id>`** — returns the transcript JSON.

**`GET /ws`** — WebSocket with the event protocol documented in
round-robin's `CLAUDE.md`. Sibling products that want live progress
should subscribe; otherwise poll the session endpoint.

### Interpreter (port 8767, planned)

**`POST /api/loop/start`** — single entry point for the user task:

```json
{
  "prompt": "<the user's original task>",
  "config": {
    "max_iterations": 5,
    "convergence_delta": 5,    // stop when improvement growth < N points
    "tokens_budget": 200000,
    "agents": [{"name":"Alpha","model":"..."}, {"name":"Bravo","model":"..."}],
    "use_pe_per_cycle": true,  // re-call PE each iteration vs Interpreter-only
    "mcp_tools": ["read_file", "write_file"]   // optional v0.3
  }
}
```

Returns either:
- `?stream=true` → SSE stream of `LoopEnvelope` per cycle, terminating with `stop_reason`.
- Otherwise: 202 + `{loop_id}` and the loop runs in the background; poll `GET /api/loop/<id>` for state.

**`POST /api/interpret`** — synthesis-only (no looping). Body:
`{transcript: TranscriptEnvelope, context: EnhancedEnvelope?}`.
Returns `{synthesis, next_prompt}`.

**`POST /api/loop/<id>/stop`** — graceful stop.

**`GET /api/loop/<id>`** — current state.

**`GET /api/health`** — same shape as PE/RR.

---

## Service discovery (`services.toml`)

Each product reads on demand from:

* Windows: `%APPDATA%\swarm\services.toml`
* Linux/macOS: `~/.config/swarm/services.toml`

```toml
[services]
prompt_enhancer = "http://127.0.0.1:8765"
round_robin     = "http://127.0.0.1:8766"
interpreter     = "http://127.0.0.1:8767"
```

Defaults to localhost loopback so dev still works without the file.
PE's `enhancer.api.discovery.get_peer_url(name, default=None)` is the
reference implementation; Interpreter and Round Robin should each
ship their own copy (modularity > DRY).

---

## Multi-machine LM Studio (LM Link)

The user's setup: **desktop** running LM Studio with gpt-oss-120b, plus
an **M5 mini** running LM Studio with a smaller model. Both connected
via LM Studio's LM Link.

How it routes:
- **All HTTP requests go to `http://localhost:1234/v1`** on the
  machine running each product. LM Studio's local endpoint internally
  routes to the remote machine when the requested `model` identifier
  lives there.
- Per-machine targeting is implicit through model names + LM Link's
  `set-preferred-device` configuration.
- **Per-host runtime override** — each product's
  `POST /api/host/use <url>` (or env var) lets the user point a
  product at a different LM Studio host without restarting. PE
  ships this via `enhancer.llm.lms_link`.

In Round Robin's UI, the dropdown tags models with their host
(green = Alpha/local, blue = Bravo/remote). Same convention should
flow into the Interpreter's UI when present.

---

## MCP integration (planned v0.3)

The user's HelpLMSmcp server (`C:\Users\Falki\HelpLMSmcp`) exposes 15
tools to LM Studio: `list_files`, `read_file`, `write_file`,
`delete_file`, `run_python`, `web_search`, `sqlite_*`, `rest_call`,
`memory_*`. Already documented at
`~/.claude/knowledge/helplms-mcp-server.md`.

Integration paths into the loop:

1. **Round Robin agents** can already produce file operations via
   Charlie (the existing implementer agent that fires on
   `\bConfirmed\b`). Charlie's sandbox is per-session under
   `data/charlie_workspace/`. MCP tools could replace or augment
   Charlie for finer-grained read access during dialogue.
2. **Interpreter** could call MCP tools during synthesis — e.g.,
   `read_file` to ground its summary in the codebase, or
   `memory_set`/`memory_get` to carry state across loop iterations.
3. **PE** doesn't need MCP for v1 (the 4-pass enhancer is pure text).

The contract: any product that wants to use MCP must call the LLM via
`provider.chat_with_tools(...)` (not yet on the standalone's
`ChatProvider`). LM Studio's MCP integration is server-side — once
`mcp.json` is registered, models that support tool-calling will emit
`tool_calls` blocks naturally. The product's job is just to surface
those tools and process the results.

---

## Versioning

`schema_version` follows `MAJOR.MINOR`:
- **MINOR bump** — adding a field. All consumers tolerate.
- **MAJOR bump** — renaming or removing a field. Each consumer must
  declare which majors it accepts; mismatched majors return HTTP 426
  Upgrade Required.

The current schema is `1.0`. Documented changes ship in each
product's CHANGELOG.md.

---

## Loop stop conditions

Interpreter's `POST /api/loop/start` config knobs (defaults shown):

| Knob | Default | Behavior |
|---|---|---|
| `max_iterations` | 5 | Hard cap; loop ends regardless of progress |
| `convergence_delta` | 5 | Stop when `improvement_trend[-1] - improvement_trend[-2] < delta` |
| `tokens_budget` | 200000 | Stop when accumulated `tokens_in_total + tokens_out_total` exceeds budget |
| `wall_time_budget_s` | 1800 | 30-minute hard wall-clock cap |
| `manual_stop` | n/a | `POST /api/loop/<id>/stop` always works |

Multiple conditions OR'd. First to fire wins; `stop_reason` carries the
trigger name.

---

## Test strategy across the four products

1. **Each product has unit tests against fakes** (PE has 53 today;
   RR has 70).
2. **An integration smoke test** lives in Interpreter's repo, since
   it's the orchestrator. It spins up PE + RR with mock LLMs and
   exercises one full loop iteration.
3. **A live test** is documented per product in its
   `STATUS.md` — manual verification against a real LM Studio.

---

## Already shipped (this repo)

- ✅ `POST /api/enhance` returning `EnhancedEnvelope`
- ✅ `GET /api/health`, `GET /api/peers`
- ✅ `services.toml` discovery layer
- ✅ Multi-host LM Studio routing (`enhancer.llm.lms_link`)
- ✅ 10 new tests covering REST + discovery (53/53 total green)

## Pending

- ⏳ Round Robin docs: `TRANSCRIPT_SCHEMA.md` (read-only — RR repo
  isn't modified by this plan, but its existing
  `data/sessions/run-*.json` shape is the canonical source).
- ⏳ Interpreter scaffold (`C:\Users\Falki\interpreter`): `/api/loop/start`
  + `/api/interpret`, real synthesis logic deferred to v0.2.
- ⏳ MCP `chat_with_tools` on `ChatProvider` (v0.3).
- ⏳ Live end-to-end loop with all three products + a real prompt.
