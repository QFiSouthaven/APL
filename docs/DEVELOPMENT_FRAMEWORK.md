# APL Development Component — Method Framework

**Status:** v0.1 reference implementation in flight at `APL/development/` (parallel build). This document is the **method framework** the implementation embodies and the v2.x evolution path.

**Cross-references:**
- Architecture vision: `C:\Users\Falki\APL\docs\ARCHITECTURE_VISION.md`
- Umbrella contract: `C:\Users\Falki\APL\README.md` (lines 47-62)
- Discovery slot: `prompt-enhancer/src/enhancer/api/discovery.py:33`
- REST mirror target: `prompt-enhancer/src/enhancer/api/rest.py:87-99`

---

## 1. Executive summary

`APL/development/` is a **stack-app generator**: the user supplies a goal statement (e.g., "URL shortener with auth and a dashboard"), and the component drives a local LLM through a structured Stage × Layer matrix to emit a complete, runnable application — frontend code, backend code, database schema, tests, docs, and deployment manifests — written to disk as a coherent file tree.

It is **not** a code-completion product like Copilot, Cursor, or Continue.dev. It does not edit your code while you type, it does not respond to inline questions about a single file, and it does not target IDE integration. It is a **batch builder** invoked once, that runs locally, and produces a multi-layer artifact set the user can then inspect, run, and iterate on.

The v0.1 reference implementation ships:
- The component skeleton (`pyproject.toml`, `src/`, `tests/`, FastAPI server)
- Discovery + REST contract mirroring prompt-enhancer (`/api/health`, `/api/peers`, `/api/build`, `/api/runs`)
- Core abstractions: `BuildRequest`, `BuildResult`, `Stage` ABC, `Orchestrator`, `MessageBoard`, `LLMClient`
- ONE end-to-end stage (Architect) that produces a structured plan
- Stub stages (Coder, Reviewer, Tester, Packager) raising `NotImplementedError` with v0.x pointers
- 25-35 tests + a placeholder web UI

---

## 2. Why a method framework

A non-trivial application is a **multi-skill, multi-layer artifact**. Building one with an LLM in a single shot fails for predictable reasons:

| Failure mode | Root cause | Framework remedy |
|---|---|---|
| Incoherent multi-file output | A single LLM call can't keep file boundaries, imports, and naming straight across hundreds of lines | Break the build into discrete stages, one task per call, with explicit handoff contracts |
| Wrong specialist | The model that's best at architectural reasoning is not the model that's best at idiomatic Python | Per-stage model selection (reuse `prompt-enhancer/src/enhancer/llm/model_router.py`) |
| Compounding errors | A bad assumption in early code propagates silently to tests + docs | Insert a Reviewer stage between Coder and Tester; allow loopback |
| Context-window overflow | A 4-layer stack (frontend + backend + db + tests) doesn't fit in one prompt | Run the same stage **per layer**, not per build; only the plan is shared |
| No checkpoint to resume | A 5-minute build that crashes wastes 5 minutes | Persist plan + per-stage outputs in MessageBoard SQLite; resume from last completed stage |

The framework's structural answer is a 2-D decomposition: **Stages × Layers**. A stage is a *kind of work* (architect, code, review, test, package). A layer is a *kind of artifact* (frontend, backend, db, tests, docs, deploy). The Orchestrator is a nested loop: outer = stages in sequence, inner = layers the stage applies to.

---

## 3. Core abstractions

Each abstraction below is the one the v0.1 implementation should ship. Signatures are illustrative (Python-flavored pseudocode); the implementation may add private fields for persistence and instrumentation.

### `BuildRequest`

The inbound shape for `POST /api/build`. A user goal plus optional hints.

```python
@dataclass
class BuildRequest:
    goal: str                              # e.g. "URL shortener with auth"
    stack_hint: str | None = None          # e.g. "fastapi+sqlite+react"
    target_lang: str = "python"            # primary backend language
    constraints: dict[str, Any] = field(default_factory=dict)
    enhance_goal: bool = False             # if True, route through prompt-enhancer first
```

Example: `BuildRequest(goal="todo app with auth", stack_hint="flask+sqlite", target_lang="python")`.

### `BuildResult`

The outbound shape from `POST /api/build`. Mirrors the envelope shape of prompt-enhancer's `EnhancedEnvelope` (`prompt-enhancer/src/enhancer/api/rest.py:61-75`).

```python
@dataclass
class BuildResult:
    schema_version: str = "1"
    build_id: str
    request: BuildRequest
    plan: dict[str, Any]                   # Architect output
    artifacts: dict[str, str]              # path -> file content
    stage_log: list[StageRecord]           # per-stage timing + tokens
    completed_at: str                      # ISO-8601 UTC
    extras: dict[str, Any] = field(default_factory=dict)
```

Example: a successful build returns `artifacts={"backend/app.py": "...", "tests/test_app.py": "...", "Dockerfile": "..."}`.

### `Stage` (ABC)

One ABC; one subclass per build phase. Names: **Architect**, **Coder**, **Reviewer**, **Tester**, **Packager**. Borrowed shape from prompt-enhancer's pass structure (`prompt-enhancer/src/enhancer/core/pipeline.py:1-15` docstring; passes encoded in `passes.py`).

```python
class Stage(ABC):
    name: str                              # "architect", "coder", ...
    @abstractmethod
    async def run(
        self,
        request: BuildRequest,
        plan: dict[str, Any],
        prior: dict[str, dict],            # outputs of prior stages, keyed by name
        layer: str,                        # which Layer this invocation targets
        llm: LLMClient,
        board: MessageBoard,
    ) -> dict[str, Any]: ...               # returns artifact subset for this (stage, layer)
```

Example: `ArchitectStage().run(req, plan={}, prior={}, layer="*", llm, board)` returns the plan dict; `CoderStage().run(req, plan, prior, layer="backend", llm, board)` returns `{"backend/app.py": "..."}`.

### `Layer` (concept; not an ABC)

Layers are namespaced strings the Architect's plan opts into. Names: **Frontend**, **Backend**, **Database**, **Tests**, **Docs**, **Deployment**. Each layer is a directory under `src/development/layers/`. Convention (v0.2+):

```python
def applies_to(plan: dict) -> bool: ...    # does this layer participate in the build?
def generate(plan: dict, llm: LLMClient) -> dict[str, str]: ...  # path -> content
```

The convention mirrors how `enhancer.transforms` plugins expose `register()` and `__call__` (see `prompt-enhancer/src/enhancer/llm/registry.py` discover_transforms section).

### `Orchestrator`

Drives a `BuildRequest` through the Stages, applying each Stage to the relevant Layers. Loop shape borrowed from `round-robin/src/round_robin/orchestrator.py:97-219` — same pause/resume/event-emit pattern, but stage-stepped instead of turn-stepped.

```python
class Orchestrator:
    def __init__(self, llm: LLMClient, board: MessageBoard, stages: list[Stage]): ...
    async def build(self, req: BuildRequest) -> BuildResult: ...
    async def stop(self) -> None: ...      # graceful shutdown
```

Example: `Orchestrator(llm, board, [Architect(), Coder(), Reviewer(), Tester(), Packager()]).build(req)`.

### `MessageBoard`

Shared event log. SQLite-backed (mirroring prompt-enhancer's `persistence/db.py` schema style). Other umbrella components subscribe via long-poll or read `/api/runs`.

```python
class MessageBoard:
    def publish(self, event_type: str, payload: dict) -> None: ...
    def subscribe(self, since_id: int = 0) -> AsyncIterator[Event]: ...
    def recent(self, limit: int = 50) -> list[Event]: ...
```

Event types (v0.1): `BUILD_STARTED`, `STAGE_STARTED`, `STAGE_PROGRESS`, `STAGE_DONE`, `STAGE_FAILED`, `BUILD_DONE`, `BUILD_FAILED`. Mirrors prompt-enhancer's `EventType` enum (`prompt-enhancer/src/enhancer/core/events.py`).

### `LLMClient`

Thin wrapper around prompt-enhancer's `LMStudioProvider`. Reuses the resilience layer (`prompt-enhancer/src/enhancer/llm/resilience.py`'s `@with_retry` / `@with_stream_retry` / `ProviderHealth`) by importing the provider directly rather than re-implementing it.

```python
class LLMClient:
    def __init__(self, base_url: str, default_model: str = ""): ...
    async def chat(self, messages: list[dict], *, model: str | None = None) -> str: ...
    async def chat_json(self, messages: list[dict], schema: dict, *, model: str | None = None) -> dict: ...
```

`chat_json` constrains output to a schema (v0.1 implementation: prompt the model with the schema and re-prompt on parse failure; v0.4+: use grammar-constrained decoding when LM Studio supports it).

---

## 4. The Stage × Layer matrix

The framework's structural backbone. Each cell describes the artifact produced when stage S is applied to layer L. Empty cells are intentional — only filled cells trigger an LLM call.

|              | Frontend | Backend | Database | Tests | Docs | Deployment |
|--------------|----------|---------|----------|-------|------|------------|
| **Architect**  | choose UI framework + component list | choose backend stack + route list | choose DB engine + table list | choose test framework + coverage targets | choose doc tooling + sections | choose deploy target + env vars |
| **Coder**      | generate component files + routes | generate route handlers + middleware | generate schema + migrations | (skip — Tester handles) | (skip — Packager handles) | generate Dockerfile + compose / scripts |
| **Reviewer**   | critique component coupling + a11y notes | critique route correctness + auth gaps | critique schema normalization + index choices | critique test fixtures + coverage gaps | critique completeness + accuracy | critique deploy security + secrets handling |
| **Tester**     | generate component / e2e tests | generate route + integration tests | generate schema-migration tests | (self — meta-tests skipped) | (skip) | generate smoke tests for built image |
| **Packager**   | bundle assets + lockfile | freeze backend deps + lockfile | bundle migration scripts | wire test runner config | render README + API.md + ARCHITECTURE.md | finalize Dockerfile + CI manifest |

**Reading the matrix:** the Orchestrator's outer loop visits the rows top-to-bottom; the inner loop visits the columns the plan opts into. A cell labelled "skip" emits a `STAGE_PROGRESS` event with `status="skipped"` so subscribers see the matrix being walked exhaustively — every (stage, layer) is visited even when no LLM call fires.

---

## 5. The Orchestrator's algorithm

Pseudocode. Not runnable. One invocation of `Orchestrator.build(req)`.

```text
async def build(self, req):
    build_id = uuid4().hex[:12]
    board.publish(BUILD_STARTED, {build_id, request: req})

    # Optional pre-step: ask prompt-enhancer to refine a fuzzy goal.
    if req.enhance_goal:
        req = await integrations.prompt_enhancer.refine(req)

    # Stage 0: Architect runs ONCE (layer="*"), produces the plan.
    plan = await architect.run(req, plan={}, prior={}, layer="*", llm, board)
    board.publish(STAGE_DONE, {stage: "architect", plan})

    # The plan declares which layers participate.
    layers = plan["layers"]                   # e.g. ["frontend","backend","database","tests","docs","deployment"]
    prior = {"architect": plan}

    # Stages 1..N run for each opted-in layer.
    for stage in [coder, reviewer, tester, packager]:
        stage_outputs = {}
        for layer in layers:
            if not stage_applies(stage, layer):           # cell is "skip"
                board.publish(STAGE_PROGRESS, {stage, layer, status: "skipped"})
                continue
            board.publish(STAGE_STARTED, {stage, layer})
            output = await stage.run(req, plan, prior, layer, llm, board)
            stage_outputs[layer] = output
            board.publish(STAGE_PROGRESS, {stage, layer, status: "done"})

        # Reviewer can request ONE loopback to Coder per layer (v0.3+).
        if stage is reviewer:
            for layer, critique in stage_outputs.items():
                if critique.requests_retry and not prior.get("coder_retried", {}).get(layer):
                    prior["coder_retried"][layer] = True
                    new_code = await coder.run(req, plan, prior_with_critique, layer, llm, board)
                    prior["coder"][layer] = new_code

        prior[stage.name] = stage_outputs

    # Assemble.
    artifacts = flatten_layer_outputs(prior)
    result = BuildResult(build_id, req, plan, artifacts, ...)
    board.publish(BUILD_DONE, {build_id, result})
    return result
```

Key invariants:
1. **Stages run serially per build.** No `asyncio.gather` across stages. Different builds may queue, but within one build the LLM gets one stage at a time. This mirrors prompt-enhancer's frozen Pass 1→Pass 2 invariant (`pipeline.py:11-12`).
2. **Reviewer loopback is bounded to one retry per layer.** Prevents infinite Coder ↔ Reviewer ping-pong.
3. **Architect runs exactly once per build.** Any additional architectural reasoning during later stages is the Reviewer's job, not the Architect's.

---

## 6. Cross-component integration

`development/` is a peer of prompt-enhancer and round-robin under the umbrella's discovery contract.

### Shared services.toml

`lab/onboarding.py:25-29` and `prompt-enhancer/src/enhancer/api/discovery.py:30-34` both already include the `development` slot at `http://127.0.0.1:8767`. No changes needed in either file — the slot is provisioned and the v0.1 component just has to honor it.

### Inbound from prompt-enhancer

Already wired. `prompt-enhancer/src/enhancer/api/rest.py:152-185` (`POST /api/forward-to/{peer}`) accepts `peer="development"` and forwards an `EnhanceRequest`-shaped body to `development`'s `/api/enhance`. Two paths to support:

| Path | Status | Notes |
|---|---|---|
| `POST /api/build` | NEW in development | Native shape: `BuildRequest`. The primary entry point. |
| `POST /api/enhance` | shim | Adapter that wraps an `EnhanceRequest` (from `rest.py:38-51`) into a `BuildRequest` and forwards. Lets the existing `/api/forward-to/development` route work without a contract change. |

### Inbound from round-robin

Round-robin reads `development`'s `/api/runs` (matching `prompt-enhancer/src/enhancer/api/rest.py:102-119`) and surfaces build progress in its discussion board. v0.1 ships the endpoint; round-robin's UI integration is a v0.2 task on round-robin's side.

### Outbound to prompt-enhancer

`development/src/development/integrations/prompt_enhancer.py` (v0.1 stub; v0.2 active) calls `prompt-enhancer/src/enhancer/api/rest.py:241-343` (`POST /api/enhance`) with the user's raw goal when `BuildRequest.enhance_goal=True`. The refined `enhanced_prompt` becomes the new `goal` before the Architect stage runs. This is the "ask prompt-enhancer to improve my prompt before I ask the architect" hop.

### Outbound to round-robin

`development/src/development/integrations/round_robin.py` (v0.1 stub; v2.x active) POSTs the `BuildResult` to round-robin for human-style discussion / review of the produced plan and artifacts. v2.x will let two LLMs critique the build out loud, with the user as moderator.

### MCP tool consumption

`development` consumes MCP tools via the existing client at `prompt-enhancer/src/enhancer/mcp/` (specifically `MCPRegistry` and `MCPToolInvoker`, exported from `prompt-enhancer/src/enhancer/mcp/__init__.py:27-39`). v0.1 imports the package; only v2.0 wires it into Stage execution.

| MCP server | Used by | Purpose |
|---|---|---|
| filesystem (read) | Architect | introspect existing project (if `BuildRequest.target_dir` points at one) to avoid clobbering; v2.0 |
| filesystem (write) | Packager | write the artifact tree to disk atomically; v2.0 |
| git | Packager | optional `git init` + initial commit; v2.0 |
| sandboxed exec | Tester | run generated tests in a sandbox to verify they pass before declaring `BUILD_DONE`; v2.0 |
| package-search | Reviewer | cross-check Architect-specified deps exist on PyPI / npm; v0.3+ |

---

## 7. Pluggability

How third parties extend the framework. Three of the four extension points already exist in prompt-enhancer; one is new.

### New Stages — reuse `enhancer.transforms`

Already shipped (v2.0). A plugin registers a `Stage` subclass in `pyproject.toml`:

```toml
[project.entry-points."enhancer.transforms"]
custom_security_review = "my_pkg:SecurityReviewStage"
```

`development` calls `prompt-enhancer/src/enhancer/llm/registry.discover_transforms()` (the same function the Pass 1-4 pipeline uses) at startup; any `Stage`-shaped object becomes a slot the Orchestrator can insert before/after the built-in five.

### New Layers — directory convention

A layer is a Python module under `src/development/layers/` that exposes:

```python
NAME: str = "my_layer"
def applies_to(plan: dict) -> bool: ...
def generate(plan: dict, llm: LLMClient) -> dict[str, str]: ...
```

The Orchestrator's layer discovery walks `src/development/layers/` and registers any module exposing those two callables. Adding a layer is a directory-drop operation — no code change to the Orchestrator.

### New LLM providers — reuse `enhancer.providers`

Already shipped (v1.2). `development` uses `prompt-enhancer/src/enhancer/llm/registry.get_provider()`, so any provider registered in either `prompt-enhancer/pyproject.toml` or a third-party `pyproject.toml` (`enhancer.providers` entry-point group) is available everywhere. No development-side change needed.

### Custom stack templates — NEW entry-point group

A new group, `development.stack_templates`, ships pre-built Architect plans. The Architect picks from registered templates when `BuildRequest.stack_hint` matches a template's declared key.

```toml
[project.entry-points."development.stack_templates"]
fastapi_sqlite_react = "my_pkg.templates:fastapi_sqlite_react_plan"
```

A template is a function returning a partial plan dict. The Architect then fills in only the layer-specific details, saving 70-80% of the architectural-reasoning tokens for known stacks.

---

## 8. v0.1 scope vs. future scope

| Feature | v0.1 | v0.2 | v0.3 | v0.4 | v0.5 | v1.0 | v2.0 |
|---|---|---|---|---|---|---|---|
| Component skeleton + FastAPI | ship | | | | | | |
| Discovery + `/api/health` + `/api/peers` | ship | | | | | | |
| `Orchestrator` + `MessageBoard` + `LLMClient` | ship | | | | | | |
| Architect stage (end-to-end) | ship | | | | | | |
| Coder stub | stub | **ship** | | | | | |
| Reviewer stub | stub | | **ship** | | | | |
| Tester stub | stub | | | **ship** | | | |
| Packager stub | stub | | | | **ship** | | |
| All five end-to-end + integration tests | | | | | | **ship** | |
| MCP tool integration (sandbox / git / fs) | | | | | | | **ship** |
| Custom stack templates entry-point | | | | | | | **ship** |
| Round-robin BuildResult review loop | | | | | | | **ship** |
| Web UI (placeholder) | ship | | | | | | |
| Web UI (live build progress) | | | **ship** | | | | |
| 25-35 tests | ship | | | | | | |
| Per-layer chunking + sliding-window summary | | | | **ship** | | | |
| Auto-resume from MessageBoard checkpoints | | | | | **ship** | | |

**v0.1 acceptance is conservative on purpose.** Shipping just Architect end-to-end exercises every cross-component contract (discovery, REST, MessageBoard, LLMClient resilience) with one real LLM call path. v0.2 then has nothing to debug except the new Coder stage itself.

---

## 9. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation | Ships in |
|---|---|---|---|---|
| Hallucinated dependencies — Architect specifies a package that doesn't exist | high | medium | Reviewer cross-checks against PyPI / npm via MCP `package-search` server | v0.3 |
| Context window overflow — 4-layer stack doesn't fit in one Stage call | high | high | Per-layer sub-calls (already the design); v0.4 adds chunking with sliding-window summary inside a layer when files exceed `~8k tokens` | v0.4 |
| Cross-layer drift — backend's `User` type doesn't match frontend's | medium | high | Architect produces a shared `types.json` document; downstream stages read from it before generating any code | v0.2 |
| Concurrency contention against single LM Studio instance | medium | low | Stages serialize per build (design); multiple builds queue at the LLMClient level via `asyncio.Lock` keyed on the LM Studio base URL | v0.1 |
| Sandboxed code execution is dangerous | low at v0.1 (no exec yet) → high at v2.0 | catastrophic | Reuse `prompt-enhancer/src/enhancer/mcp/` resilience-wrapped client; sandbox is opt-in via `BuildRequest.constraints["allow_exec"] = True`; no network by default; subprocess timeout enforced | v2.0 |
| Plan format drift between versions | medium | medium | Plan is a versioned schema (`schema_version: "1"`); consumers reject unknown versions; bump triggers a migration in the Orchestrator | v0.2 |
| Build wedges mid-stage (LM Studio crash, OOM) | medium | medium | MessageBoard SQLite checkpoints after each stage; `Orchestrator.resume(build_id)` reloads and resumes from the last `STAGE_DONE` | v0.5 |
| User goal contradicts `stack_hint` | low | low | Architect emits a warning event (`PLAN_WARNING`); proceeds with `stack_hint` taking precedence; surfaced in `BuildResult.extras` | v0.1 |
| Privacy — write artifacts to wrong directory | low | high | `BuildResult.artifacts` is in-memory in v0.1 (returned to the user, not written to disk); only v2.0's Packager + filesystem MCP writes to disk, and only inside an explicit `target_dir` whitelisted in `BuildRequest` | v0.1 / v2.0 |

---

## 10. Acceptance criteria for v0.1 ship

Concrete checklist; every item must pass before tagging `v0.1.0`.

- [ ] `pyproject.toml` exists at `APL/development/pyproject.toml` and `pip install -e .` succeeds in an isolated `.venv`
- [ ] `pytest -q` runs from `APL/development/` and reports 25-35 tests, all green
- [ ] `GET http://127.0.0.1:8767/api/health` returns `{"status":"ok","service":"development","version":"0.1.0"}` (matches `README.md` lines 51-54)
- [ ] `GET http://127.0.0.1:8767/api/peers` returns a `{"services": {...}}` body byte-for-byte the same shape as `prompt-enhancer/src/enhancer/api/rest.py:96-99`
- [ ] `POST http://127.0.0.1:8767/api/build` with `{"goal": "URL shortener with auth"}` and a real LM Studio model loaded produces a `BuildResult` whose `plan` field is non-empty JSON from the Architect stage
- [ ] All four stub stages (Coder, Reviewer, Tester, Packager) raise `NotImplementedError("ships in vX.Y — see DEVELOPMENT_FRAMEWORK.md")` and that error is surfaced through `BuildResult.extras.stub_stages`
- [ ] `python lab/launch.py` (umbrella launcher) boots prompt-enhancer + round-robin + development together; `GET /api/peers` on each component lists the other two with non-empty URLs
- [ ] Discovery defaults match across all three components — diff `prompt-enhancer/src/enhancer/api/discovery.py:30-34` vs. `round-robin/src/round_robin/discovery.py` (the byte-for-byte mirror) vs. `development/src/development/discovery.py` and confirm zero differences in the `DEFAULTS` dict
- [ ] `lab/onboarding.py` `DEFAULTS` (lines 25-29) already lists `development` — confirm and require no changes
- [ ] Web UI placeholder loads at `http://127.0.0.1:8767/` and renders an input + submit + result pane (no live event stream yet — that's v0.3)

---

## 11. Open questions

These need a decision before v0.2 work begins. Listed in priority order; the first three are blocking.

1. **Sandbox technology for MCP code-exec.** Options: `subprocess` with `resource.setrlimit` (Linux only); Docker (cross-platform but heavy); Firecracker / gVisor (Linux only, complex); WebAssembly via `wasmtime` (cross-platform, limited stdlib). Recommendation: Docker for v2.0 because the user already runs Docker Desktop (visible in the installed-apps list); revisit for v3.0.
2. **Stage parallelism within a single build.** The design says serial-per-stage, parallel-per-layer-within-stage is forbidden in v0.1. Should we revisit at v0.4 once chunking lands? Risk: parallel layer calls against one LM Studio process serialize at the LM Studio queue anyway, so the speedup is illusory — but parallel calls *across* hosts (multi-LMLink) are real. Decision needed before the cross-host scheduler ships.
3. **Plan format.** Pure JSON Schema (loose, easy to extend, hard to validate) vs. a typed dataclass we ship + version (rigid, validates well, requires migrations). Recommendation: typed dataclass + `schema_version` field; ships v0.1.
4. **Round-robin integration shape.** Webhook (push), polling (pull), or shared SQLite (read-side). The umbrella's existing pattern is pull (round-robin already polls prompt-enhancer's `/api/runs`), so consistency argues for pull. Decision can wait until v2.0.
5. **Memory: build state across crashes.** Should `MessageBoard` persist enough to resume a half-finished build after a process crash? SQLite checkpoints after each stage make this cheap; the question is whether resume is *exposed* via `/api/build/{id}/resume` in v0.5 or deferred to v1.0. Recommendation: ship the persistence in v0.1 (it's the same SQLite the events already write to), expose the endpoint in v0.5.
6. **Naming consistency.** `ARCHITECTURE_VISION.md` lines 161-167 flagged a `Development` / `interpreter` / `right-pipe/` collision. The `discovery.py` slot has since been renamed to `development` (line 33), and `lab/onboarding.py:28` agrees. Confirm `right-pipe/` is being deleted (or repurposed) before v0.1 tag — currently it's not in `ls APL/` output, so this may already be resolved.
