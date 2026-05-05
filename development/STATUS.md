# development — Build Status

_The 4th umbrella component. Local-LLM stack-app builder. Drives a real
LLM through Architect → Coder → Reviewer → Tester → Packager._

Companion docs: `APL/docs/DEVELOPMENT_FRAMEWORK.md` (method framework)
and `APL/docs/ARCHITECTURE_VISION.md` (umbrella cross-reference).

## Phase progress

| Phase | Status | Notes |
|---|---|---|
| 0 — scaffold | ✅ done | Component dir, FastAPI shell, discovery byte-for-byte mirror, MessageBoard (SQLite, async pub/sub), entry point. |
| 1 — Architect stage | ✅ done | LLM-driven stack planner; produces `ctx["plan"]` with structured `stack`, `layers`, `dependencies`, `constraints_satisfied`. |
| 2 — Coder stage | ✅ done | 4 layer generators (backend, frontend, database, deployment). Maintains both flat `ctx["artifacts"]` (path → content) and nested `ctx["artifacts_by_layer"]` (layer → {path → content}) views. |
| 3 — Reviewer stage | ✅ done | Per-layer critique + bounded one-retry Coder loopback; best-effort quality control (parse-failure falls back to approved). |
| 4 — Tester stage | ✅ done | Per-layer test generation with real subprocess execution (pytest/vitest/jest/shellcheck). 30s hard timeout. Independent bounded loopback budget from Reviewer. |
| 5 — Packager stage | ✅ done | Stack-aware Dockerfile + docker-compose.yml + .env.example + deploy.sh + deploy.ps1 + README. Hybrid: LLM generates, structural validator checks. Informational, not a build gate. |
| 6 — SSE event stream | ✅ done | `/api/events` Server-Sent Events endpoint exposes the MessageBoard live. Browser auto-reconnect at 5s; replay-then-tail via `from_id` or `Last-Event-ID`. |
| 7 — Web UI | ✅ done | Vanilla HTML/CSS/JS dark-themed. Build form, live event chips, expandable result panel. ~470 lines, zero deps. |
| 8 — Integration testing | ✅ done | `@pytest.mark.integration` test exercises the full 5-stage pipeline against a real LM Studio. Auto-skipped when LM Studio unreachable or no chat model loaded. |

## Test status (re-run 2026-05-04, Python 3.12 dev venv, v2.2.0)

```
pytest -q -m "not slow and not integration"
                                ─────────
                                222 passed, 2 deselected in 11.15s

(includes v2.1: test_reasoning_panel_wiring.py — Reviewer panel telemetry,
 and v2.2: test_panel_wiring_all_stages.py — panel routing through
 Architect/Coder/Tester/Packager + orchestrator threading.)
```

**Run integration:** with LM Studio loaded, `pytest -m integration` against this component exercises the full pipeline end-to-end.

## v1.0.0 acceptance criteria

| Criterion | Status |
|---|---|
| All 5 stages implemented end-to-end | ✅ |
| Default pipeline runs full chain | ✅ Architect → Coder → Reviewer → Tester → Packager |
| `@pytest.mark.integration` test against real LM Studio | ✅ Auto-skips when unreachable |
| Test suite green excluding integration | ✅ 139 passed, 1 deselected |
| Cross-component contract honored | ✅ `/api/health`, `/api/peers`, `/api/build`, `/api/runs`, `/api/events` |
| Discovery defaults match peer products | ✅ Byte-for-byte (`development = "http://127.0.0.1:8767"`) |
| `enhancer --version` (development equivalent) prints `1.0.0` | ✅ |
| Tag `development-v1.0.0` on the release commit | ✅ Attached after release commit |

## v2.0 additions

| Capability | Status | Notes |
|---|---|---|
| MCP-style tools in Coder | ✅ done | Opt-in `tool_use=True`. Catalog: `fs_read`, `fs_list`, `git_status`, `git_log`, `git_diff`, `sandboxed_exec`. Hard cap 5 tool calls/layer. Per-layer `tempfile.TemporaryDirectory` sandbox. `_ALLOWED_BINS` whitelist for shell exec. Default `tool_use=False` preserves v1.0 behavior. |
| Stack templates entry-point | ✅ done | `discover_templates()` walks `development.stack_templates` group. Built-in `fastapi-sqlite` template registered same way third parties will. Architect fast-path skips LLM when hint matches a registered template. |
| Round-robin reviewer alternate | ✅ done | `BuildRequest.reviewer = "round-robin"` swaps the Reviewer for `RoundRobinReviewer` per-build. Round-robin's `/api/review` shipped (v2.1) — deferred-mode fallback only triggers on connection failure now. Shared `_reviewer_loopbacks` budget across both reviewer kinds. |

## v2.1 + v2.2 additions

| Capability | Status | Notes |
|---|---|---|
| ReasoningPanel in Reviewer (v2.1) | ✅ done | `Stage.__init__` accepts `reasoning_panel`; Reviewer routes critique through `panel.consult` when supplied. Per-slot raw outputs surface in `ctx["review"][layer]["panel"]` with `{primary, partners: [...]}` shape. |
| ReasoningPanel in all 5 stages (v2.2) | ✅ done | Architect/Coder/Tester/Packager all route LLM calls through the panel when supplied. Coder's tool_use=True flow does ONE planning consult per layer before the tool loop runs unchanged (partners can't coherently emit tool_calls into a shared sandbox). Default `reasoning_panel=None` everywhere preserves v2.0 behavior. |

## Roadmap forward

```
v1.0  ✅ five-stage pipeline + integration test
v2.0  ✅ MCP tools + stack templates + round-robin reviewer
v2.1  ✅ ReasoningPanel wired into Reviewer
v2.2  ✅ ReasoningPanel wired into all 5 stages                    ← YOU ARE HERE
v2.x  ⏳ extracted-to-shared-lib LLM provider (apl-llm)
       + pluggable stage discovery
       + plan-format schema_version migrations
       + native LMStudio chat_with_tools (provider method, not message-fallback)  ✅ shipped in prompt-enhancer v2.1
       + Pass 3 streaming-panel aggregation
```
