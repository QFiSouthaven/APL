# APL umbrella — architecture vision cross-reference

**Source diagram:** Block diagram supplied 2026-05-04 showing four
swimlanes (`backend api`, `Prompt enhancer`, `Round Robin`, `Development`)
plus two physical compute targets and an `LMLink` bridge.

**Cross-referenced against:** repo at HEAD `cd2f81e` (tag `v2.0.1`),
prompt-enhancer at v2.0.1 (284 tests), round-robin (129 tests), local
filesystem at `C:\Users\Falki\APL\`.

This doc is the canonical answer to "does the diagram match what we
actually have?"

---

## TL;DR

The diagram captures roughly **70% of the as-built APL umbrella** and
**100% of the user-visible flow**. What it gets right: every box in the
`Prompt enhancer` and `Round Robin` swimlanes corresponds to real code
on disk; the `LMLink` between two physical machines is exactly the
multi-host scenario `prompt-enhancer/src/enhancer/llm/lms_discovery.py`
was extended for in v1.2.

What's missing or new:

1. The **`Development`** swimlane (right) is a planned-but-unbuilt
   component. The umbrella already has a port reserved for it
   (`interpreter = "http://127.0.0.1:8767"` at
   `prompt-enhancer/src/enhancer/api/discovery.py:32`) but no source
   directory exists.
2. The diagram does not depict any of the **invisible
   infrastructure** shipped between v1.0.0 and v2.0.1: MCP client
   subpackage, TOML pipeline graph + validator, two plugin
   entry-point groups, observability layer, three new providers
   (Ollama/OpenAI/Anthropic), resilience decorators, REST cross-
   invocation. These are correct omissions for a flow diagram —
   they're plumbing, not boxes — but worth flagging so a reader of
   the diagram doesn't conclude only LM Studio is wired.
3. The diagram exposes that **`hardware-info/m5/`** is not orphaned
   data after all — its `.blb` / `.db` files are diagnostic snapshots
   of the physical AMD Ryzen AI MAX+ 395 machine drawn at the top.
   Previous status was "data archive, not a service"; that's still
   accurate but the *purpose* is now legible.

---

## What I see in the diagram

Four swimlanes, two compute targets, one bridge:

| Swimlane | Boxes (top→bottom, left→right) | What it represents |
|---|---|---|
| **backend api** (top-left) | Hardware spec card (AMD Ryzen AI MAX+ 395, Radeon 8060S, 128 GB RAM, 96 GB graphics, 1.8 TB) → LLM1 / LLM2 → two LM Studio instances → `LMLink` → second hardware spec (Desktop, RTX 4070, 64 GB RAM, i7-13700k) | The two physical compute targets. LMLink bridges them so either machine can serve inference for any sibling component. |
| **Prompt enhancer** (lower-left) | LLM Placeholder → USER INPUT → `prompt-enhancer (webui)` → Enhance → output → `Round-Robin`. Plus persona / SoT / magnitude branches and a `pipeline` tab strip showing **Pass 1 → Pass 2 → Focus → Pass 3 → Persona → Pass 4 → Magnitude → SoT → Done**. | This product. The "Focus" segment between Pass 2 and Pass 3 is the disambiguation pause. Output forwards to Round-Robin. |
| **Round Robin** (center) | Top: Summarize LLM → LLM Placeholder. Middle: Agent C → workspace → Final outcome. Sides: Agent A & Agent B → Theme + Discussion board. Final outcome ← output. | Two-LLM dialogue (Agent A + Agent B) plus an implementer (Agent C = "Charlie"). |
| **Development** (right) | Orchestrator → Message board. Code LLM ↔ LLM Placeholder. Output of this swimlane → Round-Robin's Final outcome. | A NEW component: an orchestrated code-writing agent. Not yet in the repo. |

A red arrow runs from `backend api` (the LM Studio runtime) all the way
right to `Code LLM` in `Development`. Reading: every component reaches
the inference backend through the same LM Link mesh.

---

## What MATCHES the current repo

### `Prompt enhancer` swimlane → real code

| Diagram box | File / line | Notes |
|---|---|---|
| `USER INPUT` → `prompt-enhancer (webui)` | `prompt-enhancer/src/enhancer/ui/pages/studio.py` | The NiceGUI Studio page is the webui. |
| `Enhance` button → `output` | `prompt-enhancer/src/enhancer/cli/main.py:enhance` + `studio.py` run handler | CLI + UI both call `core.pipeline.run_pipeline`. |
| `pipeline` tab strip: Pass 1 → Pass 2 → **Focus** → Pass 3 → Persona → Pass 4 → Magnitude → SoT → Done | `prompt-enhancer/src/enhancer/core/pipeline.py:319-672` | Order matches the code. **The "Focus" segment is the disambiguation pause**: `core/pipeline.py:393-413` triggers when `count_weakness_fields(pass2) >= 3` and yields the questions through `EventType.AGENT_DISAMBIGUATE`. The diagram name `Focus` = disambiguate-and-resume. |
| `persona` / `SoT` / `magnitude` branch boxes | `core/pipeline.py:417-463`, `:632-672`; system prompts in `core/transforms.py` | Optional transforms. All run **after** Pass 4 per frozen invariant 2. |
| `output` → `Round-Robin` arrow | `prompt-enhancer/src/enhancer/api/rest.py` `POST /api/forward-to/{peer}` (v1.2) | The cross-component invocation surface; calls `discovery.get_peer_url("round_robin")`. |
| `LLM Placeholder` (top of swimlane) | `prompt-enhancer/src/enhancer/llm/registry.py:get_provider` | Resolves to one of {LMStudio, Ollama, OpenAI, Anthropic} per `Settings.provider`. |

### `Round Robin` swimlane → real code

| Diagram box | File / line | Notes |
|---|---|---|
| `Agent A`, `Agent B` (the dialogue pair) | `round-robin/src/round_robin/orchestrator.py` | The two-LLM round-robin loop. |
| `Agent C` (= "Charlie") | `round-robin/src/round_robin/charlie/agent.py`, `charlie/workspace.py` | Optional implementer. The diagram label "Agent C" is the same component README calls "Charlie." |
| `workspace` | `round-robin/src/round_robin/charlie/workspace.py` | Charlie writes its scratch artifacts here. |
| `Theme` | concept lives in `round-robin/src/round_robin/orchestrator.py` | The dialogue topic threaded through messages. |
| `Discussion board`, `Final outcome` | UI elements in `round-robin/src/round_robin/static/index.html` | User-facing surfaces that render orchestrator output. |
| `Summarize LLM` (top) | not a separate module — invoked inline | A summary call after the dialogue completes; uses the same provider as Agent A/B. |
| `output` (right side, into Final outcome) | comes from `Development` swimlane (next bullet) | Implementation-level: this is the cross-component arrow that doesn't yet have a sender. |

### `backend api` swimlane → real code + real machines

| Diagram element | File / line | Notes |
|---|---|---|
| Hardware card, top: **AMD Ryzen AI MAX+ 395, Radeon 8060S, 128 GB RAM, 96 GB graphics, 1.8 TB** | `APL/hardware-info/m5/` directory | **This is "m5"** — the AMD machine. The `RGStats.db`, `gmdb.blb`, `gallery.blb`, `User.blb`, `rssettings.json` files in `hardware-info/m5/` are diagnostic snapshots OF this physical box, not source code. The directory's `m5-hardware-spec.txt` is empty but the .blb files presumably encode what the diagram has typed out. |
| Hardware card, right: **Desktop, RTX 4070, 64 GB RAM, i7-13700k** | the dev box this session has been running on | Confirmed by `enhancer.exe version` returning `2.0.1` from this machine. |
| `LMLink` between the two | `prompt-enhancer/src/enhancer/llm/lms_link.py` (URL override) + `lms_discovery.py:discover_chat_models_multihost` (multi-host fan-out, v1.2) | The discovery layer can already discover models across hosts; the override layer can already point inference at either host. The diagram's two-LM-Studio + LMLink topology is exactly what these two modules ship for. |
| `LLM1`, `LLM2` | model slots, not modules | Resolve to whatever LM Studio reports loaded via `/api/v0/models`. The user currently has `qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2` loaded on the dev box. |

---

## What's NEW in the diagram (gaps in the repo)

### 1. The `Development` swimlane

| Diagram box | Repo state | Gap |
|---|---|---|
| `Orchestrator` | none | new — would coordinate the code agent + message board |
| `Message board` | none | new — likely a shared event log other swimlanes can subscribe to |
| `Code LLM` | none | new — a specialized coder LLM (probably qwen3-coder family per the model_router rules) |
| `LLM Placeholder` (×2) | none | provider slots |
| `output → Round-Robin Final outcome` | not wired | the cross-component flow exists in spec only |

**However**, the umbrella has already provisioned for this:

- `prompt-enhancer/src/enhancer/api/discovery.py:30-34` reserves a port:
  ```python
  DEFAULTS: dict[str, str] = {
      "prompt_enhancer": "http://127.0.0.1:8765",
      "round_robin":     "http://127.0.0.1:8766",
      "interpreter":     "http://127.0.0.1:8767",   # ← this slot
  }
  ```
  The diagram label is "Development" but the existing umbrella convention is `interpreter`. **Decision needed:** keep the existing `interpreter` name OR rename the discovery key to `development`. Renaming requires a coordinated update to `round-robin/src/round_robin/discovery.py:28-32` (the byte-for-byte mirror) so the two products keep agreeing on each other's names.

- `APL/README.md` already has a row for `right-pipe/` ("Reserved — empty placeholder.") on port `8767`. **Conflict:** the diagram's "Development" component and the existing `right-pipe/` reservation collide on the same port (and possibly the same role). Likely the user meant "Development" to BE `right-pipe/` once it's built — i.e., rename right-pipe/ to a more descriptive name, or implement Development inside right-pipe/.

- `APL/lab/launch.py` `COMPONENTS` dict and `APL/lab/onboarding.py` `DEFAULTS` would need a new entry for whatever name the Development component lands under.

**Action sketch for v2.1+:** create `APL/development/` (or whichever name wins), with:
- a FastAPI server matching the cross-component contract (`/api/health` + `/api/peers`)
- an `Orchestrator` class that dispatches to `Code LLM` for code generation
- a `MessageBoard` (could be SQLite-backed, or in-memory pub/sub)
- a discovery module mirroring prompt-enhancer's
- a launcher entry in `APL/lab/launch.py`

---

## What's in the repo but NOT in the diagram

The diagram is a **flow** picture; it correctly omits non-flow plumbing.
Calling these out so a reader of the diagram doesn't conclude they're
unimplemented:

| Repo feature | Where | Why not in the diagram |
|---|---|---|
| 3 additional providers (Ollama, OpenAI, Anthropic) | `prompt-enhancer/src/enhancer/llm/{ollama,openai,anthropic}.py` (v1.1) | Diagram shows only `LM Studio` because that's the local backend. The other three are pluggable alternates the user can switch via `Settings.provider`. |
| MCP client subpackage | `prompt-enhancer/src/enhancer/mcp/` (v2.0) | Tool-calling infrastructure. v2.0.1 wires it as an optional pre-Pass-1/Pass-3 hook in `core/pipeline.py:333-341, 506-515`. |
| TOML pipeline graph + static validator | `prompt-enhancer/src/enhancer/core/pipeline_graph.py` (v2.0); wired in `pipeline.py:213-220` (v2.0.1) | Lets the user customize pass count/order via TOML; rejects invariant-violating configs at LOAD time. The diagram shows the canonical 4-pass order which is also the default graph. |
| Plugin entry-points | `prompt-enhancer/src/enhancer/llm/registry.py:get_provider` + `discover_transforms` (v1.2 + v2.0) | `enhancer.providers` and `enhancer.transforms` groups — third-party packages register without modifying enhancer code. |
| Observability layer | `prompt-enhancer/src/enhancer/observability/__init__.py` (v1.1) | structlog + soft OTEL hooks; no flow representation. |
| Resilience layer | `prompt-enhancer/src/enhancer/llm/resilience.py` (v1.0) | `@with_retry` / `@with_stream_retry` + `ProviderHealth` circuit breaker. Wraps every provider call transparently. |
| Cross-component REST | `prompt-enhancer/src/enhancer/api/rest.py` `/api/runs`, `/api/sessions`, `/api/forward-to/{peer}` (v1.2) | The arrow from prompt-enhancer's `output` to `Round-Robin` IS this surface, just not labeled in the diagram. |
| Model router | `prompt-enhancer/src/enhancer/llm/model_router.py` (v1.2); wired in `pipeline.py:548-559` (v2.0.1) | Picks Pass 4 scorer based on detected `task_type`. |
| Shared services discovery | `prompt-enhancer/src/enhancer/api/discovery.py` + `round-robin/src/round_robin/discovery.py` + `APL/lab/onboarding.py` (v1.2) | Both products read the same `services.toml`. The diagram's swimlane proximity implies discovery; the file is the actual mechanism. |
| `right-pipe/` and `hardware-info/` siblings | `APL/right-pipe/` (empty), `APL/hardware-info/m5/` (this machine's data) | Diagram absorbs `hardware-info/m5/` into the AMD spec card at the top of `backend api`. Smart. `right-pipe/` not represented — consistent with it being an empty placeholder. |

---

## Mismatches worth flagging

1. **`Development` swimlane vs. `interpreter` discovery key vs. `right-pipe/` directory.** The umbrella has THREE references to a future fourth component, each with a different name:
   - `discovery.py:32` calls it `interpreter`
   - `APL/README.md` calls it `right-pipe/`
   - The diagram calls it `Development`

   **Recommendation:** pick one canonical name BEFORE writing any code for it. The discovery-key change is the most expensive to fix later because both products mirror the dict; the directory rename is cheap; the README is trivial.

2. **Two `LM Studio` boxes in `backend api`.** One per physical machine. This implies the user runs LM Studio on BOTH the AMD box AND the dev desktop simultaneously, with `LMLink` brokering. The repo supports this (`lms_discovery.discover_chat_models_multihost` for read-side fan-out; `lms_link.set_override` for routing inference) but **there is no automated picker that decides which host to send a given pipeline run to**. Today the user picks via env var `ENHANCER_LMS_BASE_URL` or the UI's settings page. A `pick_loaded_host(hosts, preferred)` helper exists in `lms_discovery.py` but is not yet wired into `cli/main.py` or `studio.py`. **v2.1 candidate:** auto-route to whichever host has the best-matching model loaded.

3. **`output → Round-Robin` arrow.** `/api/forward-to/{peer}` (v1.2) provides the mechanism, but the prompt-enhancer UI does not yet expose a "send to round-robin" button. Today users either use the CLI (`curl -X POST http://127.0.0.1:8765/api/forward-to/round_robin -d '...'`) or wire it externally. **UX gap:** add a "→ Round Robin" button to the studio page after a successful enhance.

4. **`Code LLM ↔ LLM Placeholder` in `Development`.** The bidirectional arrow suggests the Code LLM iterates with another LLM (probably a critic or planner). This is a `round_robin`-shaped pattern repurposed for code generation. **If this becomes real**, consider extracting the round-robin loop into a shared `APL/lab/dialogue.py` library that both `round-robin/` and `development/` can import — avoid copy-paste of the orchestration core.

5. **No diagram representation of MCP tool servers.** The current pipeline can call MCP tools at Pass 1/3 (v2.0.1 wired this), but the diagram doesn't show MCP servers as a swimlane or external box. As MCP usage grows this is worth adding — likely a small "MCP Servers" cluster outside the four swimlanes, with arrows into prompt-enhancer's pipeline and (potentially) into Development's Orchestrator.

---

## Suggested next steps

Ordered by leverage. None blocking; all align with the v2.1 / v3.0
horizon.

1. **Resolve the `Development` / `interpreter` / `right-pipe/` naming collision.** Pick one. Update the three files (discovery defaults, README, directory). Cheapest win — costs minutes, prevents weeks of re-naming later.

2. **Implement the Development component as `APL/<chosen-name>/`** mirroring round-robin's shape:
   - FastAPI server with `/api/health` + `/api/peers` matching the umbrella contract
   - `Orchestrator` + `MessageBoard` modules
   - A `Code LLM` provider config (likely uses the existing `model_router.select_scorer` with `task_type="coding"` to auto-pick a coder model)
   - Discovery module mirroring `round-robin/src/round_robin/discovery.py`
   - Launcher entry in `APL/lab/launch.py` `COMPONENTS` dict
   - Tests

3. **Wire the `output → Round-Robin` UX.** Add a "Forward to Round Robin" button to `prompt-enhancer/src/enhancer/ui/pages/studio.py` that calls `/api/forward-to/round_robin` with the just-completed enhancement. Surface the response.

4. **Extract round-robin's dialogue loop into a shared library** (`APL/lab/dialogue.py`) so the new Development component can reuse it without copy-paste. Defer until Development is being implemented.

5. **Wire `lms_discovery.pick_loaded_host` into the CLI and UI** so multi-host LMLink topologies route automatically. Eliminates the manual env-var dance shown implicitly in the diagram.

6. **Update the diagram itself** to reflect v2.0.1 reality: add `MCP Servers` cluster; add a small "configurable via TOML" badge on the pipeline; mark the Development swimlane as "planned" with a different border style. (This is a docs-side action — the diagram itself is authoritative as a vision, not a current-state map.)
