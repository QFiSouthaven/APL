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

## Test status (re-run 2026-05-04, Python 3.12 dev venv, v2.0.0)

```
tests/test_architect_stage.py    6 passed   (JSON parse paths + retry)
tests/test_coder_stage.py       13 passed   (per-layer dispatch + skips)
tests/test_discovery.py          7 passed   (byte-for-byte mirror of prompt-enhancer)
tests/test_endpoints.py          9 passed   (/api/health, /api/peers, /api/build, /api/runs, /api/events)
tests/test_layer_generators.py  25 passed   (3 per generator × 4 + parser unit tests)
tests/test_messageboard.py       8 passed   (publish/subscribe/recent + threading)
tests/test_orchestrator.py       8 passed   (default 5-stage pipeline + chain semantics)
tests/test_packager_stage.py    21 passed   (5 stack permutations + 4 validator paths)
tests/test_reviewer_stage.py    12 passed   (bounded loopback + parse fallback)
tests/test_sse_events.py         9 passed   (SSE wire format + reconnect)
tests/test_static_ui.py          4 passed   (HTML well-formed + endpoint references)
tests/test_tester_stage.py      16 passed   (15 fast + 1 slow real-pytest)
tests/test_integration_lmstudio.py 1 passed (helper) + 1 deselected (real LM Studio; opt-in via -m integration)
tests/test_tools.py             19 passed   (filesystem/git/exec sandbox + traversal guards)
tests/test_coder_tool_use.py     8 passed   (tool_use opt-in + budget cap + dispatch)
tests/test_stack_templates.py   21 passed   (discover_templates + fastapi-sqlite + Architect fast-path)
tests/test_round_robin_reviewer.py 15 passed (alternate reviewer + deferred-mode fallback)
                                ─────────
                                202 passed, 1 deselected in 11.31s
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
| Round-robin reviewer alternate | ✅ done | `BuildRequest.reviewer = "round-robin"` swaps the Reviewer for `RoundRobinReviewer` per-build. Deferred-mode fallback: round-robin's `/api/review` doesn't exist yet, so each layer falls back to single-pass with a `STAGE_PROGRESS` event noting `deferred=True`. Shared `_reviewer_loopbacks` budget across both reviewer kinds. |

## Roadmap forward

```
v1.0  ✅ five-stage pipeline + integration test
v2.0  ✅ MCP tools + stack templates + round-robin reviewer       ← YOU ARE HERE
v2.x  ⏳ extracted-to-shared-lib LLM provider (apl-llm)
       + pluggable stage discovery
       + plan-format schema_version migrations
       + native LMStudio chat_with_tools (provider method, not message-fallback)
       + round-robin /api/review endpoint to flip the deferred-mode fallback
```
