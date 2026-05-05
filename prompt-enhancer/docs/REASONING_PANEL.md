# ReasoningPanel — N-slot LLM panel

**Status:** shipped in prompt-enhancer v2.2 (commit `7f58aad`), development v2.2.0,
round-robin (panel-per-voice in `/api/review`).

`ReasoningPanel` is the canonical home for the architecture-vision diagram's
"(+ LLM Placeholder)" boxes — the slots where every umbrella component can
wire one or more reasoning-partner LLMs alongside its primary. Source of
truth: `src/enhancer/llm/reasoning_panel.py`.

---

## 1. What it is

A panel is an ordered list of `LLMSlot`s. Slot 0 is the primary (its output
is canonical); slots 1..N are partners. The panel is consulted via
`await panel.consult(messages, mode=..., aggregator=...)`, which returns a
`PanelResult` carrying both the aggregated text and per-slot raw outputs
for observability.

The panel is the cross-component abstraction. Each component opts in via
an optional `reasoning_panel=...` parameter on its main entry point;
`reasoning_panel=None` (default everywhere) means byte-identical pre-v2.1
single-LLM behavior.

## 2. Mental model

- **Slot 0 is primary.** Its content is what the user sees; partners are
  advisory unless the aggregator says otherwise.
- **Slots 1..N are partners.** Critics, alternative perspectives, second
  opinions — each is an independent `ChatProvider` instance with its own
  model, host, weight, role.
- **Heterogeneous panels are first-class.** Different providers, different
  LM Studio hosts, different models per slot are the design point.
- **Panel size is unbounded.** A panel of one is just a primary; a panel
  of fifty is a full deliberation circle. List semantics, no cap.

## 3. Three modes

Pass via `mode="..."` to `panel.consult` or to the component's panel
parameter (e.g. `panel_mode="parallel"`).

| Mode | What it does | When to use |
|---|---|---|
| `primary-only` | Instantiates partners but doesn't call them. Panel acts like a plain primary. | Wire panels everywhere now, decide per-call when to actually engage them. |
| `parallel` | `asyncio.gather`s every slot. Aggregator reduces. | Default for critique-style work — fastest, bounded by slowest slot. |
| `sequential` | Primary runs; each partner sees prior outputs as appended assistant turns. | Genuine cross-model chain-of-thought; costs N times wall-clock. |

## 4. Three aggregators

Pass via `aggregator="..."` (or `panel_aggregator="..."`).

| Aggregator | What it returns | When to use |
|---|---|---|
| `primary-wins` | Primary's content verbatim. Partners are advisory. | Default — preserves single-LLM semantics; partners enrich telemetry only. |
| `longest` | The non-error response with the most characters. | "Most thorough thinker wins"; falls back to primary on all-error. |
| `consensus-vote` | Per-key majority across slots that produced parseable JSON dicts. Weighted by `slot.weight`. Falls back to primary-wins if fewer than two slots parsed. | Categorical / boolean decisions where slots emit JSON. |

## 5. Wiring it into prompt-enhancer

`run_pipeline` accepts `reasoning_panel`, `panel_mode`, `panel_aggregator`.
When supplied, Pass 1 / Pass 2 / Pass 4 route through `panel.consult`;
Pass 3 streams the primary's tokens live and runs partners non-streaming
in parallel for telemetry only (see "Pass 3 streaming caveat" below).

```python
import asyncio

from enhancer.core.pipeline import run_pipeline, PipelineOptions
from enhancer.llm.lmstudio import LMStudioProvider
from enhancer.llm.reasoning_panel import LLMSlot, ReasoningPanel


async def main() -> None:
    # Two providers — local Alpha at default port, remote Bravo via LM Link.
    alpha = LMStudioProvider()  # http://127.0.0.1:1234/v1
    bravo = LMStudioProvider(base_url="http://192.168.1.42:1234/v1")

    panel = ReasoningPanel([
        LLMSlot("primary", alpha, "qwen3-coder", role=""),
        LLMSlot(
            "critic", bravo, "deepseek-r1",
            role="rigorous code critic", weight=1.5,
        ),
        LLMSlot(
            "alt", alpha, "llama-3.1-70b",
            role="alternative perspective",
        ),
    ])

    result = await run_pipeline(
        prompt="Make me a customer-support chatbot for a SaaS startup",
        provider=alpha,
        model="qwen3-coder",
        opts=PipelineOptions(),
        reasoning_panel=panel,
        panel_mode="parallel",
        panel_aggregator="primary-wins",
    )

    # Aggregated/canonical outputs are exactly the v2.0 shape.
    print(result.result)            # the enhanced prompt
    print(result.scores)            # Pass 4 scores

    # Per-pass partner telemetry lives in extras["panel"].
    panel_tel = (result.extras or {}).get("panel", {})
    for pass_key in ("pass1", "pass2", "pass3", "pass4"):
        info = panel_tel.get(pass_key)
        if not info:
            continue
        print(f"\n[{pass_key}] primary len = {len(info['primary'])}")
        for partner in info["partners"]:
            tag = partner["error"] or f"{len(partner['content'])} chars"
            print(f"  {partner['name']:<10} {partner['ms']:>5} ms  {tag}")


asyncio.run(main())
```

The keys you can read from `result.extras["panel"]`:
`pass1`, `pass2`, `pass3`, `pass4` — only the ones that ran. Each value is
the canonical `{"primary": <str>, "partners": [{"name", "content", "ms",
"error"}]}` shape (see section 9).

## 6. Wiring it into development

The `Orchestrator` accepts a `reasoning_panel` kwarg and threads it into
every default stage. Each stage that opts in routes its primary LLM call
through the panel; when `reasoning_panel=None` (default), every stage's
behavior is byte-for-byte identical to v2.0.

```python
from development.llm_client import LLMClient
from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.types import BuildRequest

# Same `panel` constructed exactly as in section 5 (LLMSlot + ReasoningPanel).
llm = LLMClient()
board = MessageBoard("./build-events.sqlite")

orch = Orchestrator(llm, board, reasoning_panel=panel)
result = await orch.build(BuildRequest(goal="A small JWT auth service"))

# Per-stage telemetry surfaces in ctx; the canonical keys are:
#   ctx["architect_panel"], ctx["coder_panel"], ctx["tester_panel"],
#   ctx["packager_panel"]
# The Reviewer keeps its per-layer shape: ctx["review"][layer]["panel"].
```

Coder caveat: when `tool_use=True`, only the primary participates in the
tool-call loop (partners can't coherently emit tool_calls into a shared
sandbox). Coder does ONE planning consult per layer through the panel,
then runs the existing tool loop unchanged on the primary.

## 7. Wiring it into round-robin

The four-voice review (Agent A / B / C / Consensus) accepts
`reasoning_panel=...`. When supplied, **each** voice consults the panel
instead of the single `lm_client`, so every voice itself becomes a panel
of N reasoning slots.

```python
from round_robin.code_review import review_with_dialogue
# Re-export of the canonical panel from prompt-enhancer:
from round_robin.reasoning_panel import LLMSlot, ReasoningPanel

from pathlib import Path

verdict = await review_with_dialogue(
    layer="services/auth",
    purpose="JWT issuance and refresh",
    files={"auth.py": Path("auth.py").read_text(encoding="utf-8")},
    reasoning_panel=panel,        # built as in section 5
)

# Same response shape as the non-panel path:
#   {"approved": bool, "request_regenerate": bool,
#    "issues": [...], "summary": str,
#    "agents": {"agent_a_verdict", "agent_b_verdict",
#               "agent_c_verdict", "consensus"}}
```

## 8. Pass 3 streaming caveat

Pass 3 (the rewrite) streams its tokens live to the UI. Aggregating
streams across heterogeneous providers has no UX-coherent answer, so the
v2.2 design is:

- **Primary's tokens stream live**, exactly as before — the user sees the
  rewrite token-by-token.
- **Partners run non-streaming `provider.chat` calls in parallel** for
  telemetry only.
- **Pass 3 is `primary-wins` by design**, regardless of `panel_aggregator`.
  The `panel_aggregator` setting still applies to Pass 1 / 2 / 4.

Partner output for Pass 3 lands in `extras["panel"]["pass3"]` with the same
`{primary, partners: [...]}` shape as the other passes; bounded by
`request_timeout` so an unreachable partner can't pin Pass 4.

## 9. Telemetry shape

Every component flattens panel output to the same canonical dict so
downstream tools don't have to special-case per component.

```python
{
    "primary": "<aggregated text the pipeline parsed>",
    "partners": [
        {
            "name": "<slot name>",
            "content": "<this slot's raw output>",
            "ms": <int wall-clock>,
            "error": <str or None>,
        },
        ...
    ],
}
```

Where it lives per component:

| Component | Location |
|---|---|
| prompt-enhancer | `result.extras["panel"][<pass_key>]`, where `pass_key` is `pass1`/`pass2`/`pass3`/`pass4`. |
| development | `ctx["<stage>_panel"]` for Architect/Coder/Tester/Packager; Reviewer uses `ctx["review"][layer]["panel"]`. |
| round-robin | The four-voice response's `agents` block grows: `agent_a_verdict`, `agent_b_verdict`, `agent_c_verdict`, `consensus`. |

## 10. Failure tolerance

- A partner whose `provider.chat` raises gets a `SlotResponse` with
  `error="<ExceptionClass>: <msg>"` and empty content. The panel call
  itself never raises from a partner failure — see
  `tests/test_reasoning_panel.py::test_partner_failure_does_not_kill_panel`.
- The primary failing IS surfaced (its `SlotResponse.error` is non-None);
  components handle that the same way they handle a single-LLM failure.
- Pass 3 partner timeouts are bounded by `request_timeout` and reported
  per-slot as `"error": "TimeoutError"`.

## 11. Performance notes

- `parallel` mode is bounded by the slowest slot's wall-clock.
- Heterogeneous providers (different LM Studio hosts) spread load across
  GPUs and avoid LM Studio's per-model serialization.
- `primary-only` mode is the cheapest opt-in: it instantiates partner
  slots but never calls them — useful as a rollout flag.
- `sequential` mode multiplies wall-clock by the number of slots.

## 12. Limitations

- **Coder's `tool_use=True` is text-only for partners.** Partners can't
  coherently emit `tool_calls` into a shared sandbox, so Coder issues a
  single planning consult per layer through the panel, then runs the
  existing tool-call loop unchanged on the primary.
- **Pass 3 aggregation is `primary-wins` by design.** Streaming
  aggregation across heterogeneous providers is a UX problem, not just an
  engineering one; v2.2 doesn't try to solve it.
- **`consensus-vote` requires JSON object responses.** Slots whose output
  doesn't `json.loads()` to a dict are skipped; if fewer than two slots
  parse, the aggregator falls back to primary-wins.

## See also

- `src/enhancer/llm/reasoning_panel.py` — the implementation; the module
  docstring is the canonical spec.
- `tests/test_reasoning_panel.py` — usage patterns for every mode and
  aggregator.
- `tests/test_pipeline_panel.py` — pipeline-level wiring tests.
- `development/STATUS.md` — v2.1 + v2.2 phase rows for the development
  side.
- round-robin's `src/round_robin/code_review.py` — `_review_with_panel`
  is the four-voice + panel-per-voice implementation.
