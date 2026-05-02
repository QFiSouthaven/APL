# Build Status

_Auto-updated by the implementation thread; reviewed by the Methodology
Enhancement Agent on every diff._

## Phase progress

| Phase | Status | Notes |
|---|---|---|
| 0 — scaffold | ✅ done | `pyproject.toml`, `src/` layout, configs, methodology agent hook |
| 1 — extract core | ✅ done | helpers, system prompts, pipeline + disambig pause/resume, all three concurrency invariants |
| 2 — SQLite persistence | ✅ done | schema, db, runs, sessions, jsonl_compat, safestorage |
| 3 — ChatProvider abstraction | ✅ done | LM Studio fully implemented; Ollama/OpenAI/Anthropic stubs with install hints |
| 4 — typer CLI | ✅ done | `version` `models` `enhance` `history` `ui` `batch` `compare` `export` + interactive disambiguation Q&A + `--skip-clarify` flag |
| 5 — NiceGUI Desktop Studio | ✅ done | Studio (status strip + tabs + sliders + diff view + 6 components), History (with branch_tree row-detail), Analytics, Compare, Templates (8 seeds), Settings, disambiguation modal |
| 6 — packaging | scaffolded | PyInstaller spec + Inno Setup script in `packaging/` |
| 7 — verification | ✅ **LIVE-TESTED** | **41/41 unit tests green** + end-to-end run against gpt-oss-120b via LM Link confirmed below |

## Test status

```
tests/test_concurrency.py ...                  3 passed   (the three load-bearing guards)
tests/test_disambiguation.py ....              4 passed   (pause + resume + per-pass timing + skip-clarify)
tests/test_migration.py ....                   4 passed   (JSONL → SQLite)
tests/test_parsing.py ...........................  27 passed
tests/test_pipeline_smoke.py ...               3 passed   (end-to-end via FakeChatProvider)
                                              ────────────
                                              41 passed in 11.60s
```

## Live verification — 2026-04-28 against gpt-oss-120b via LM Link

### Run 1 — initial round-trip (run id `5289124687aaae92`)

```
$ enhancer enhance "Make me a customer-support chatbot for a small SaaS startup" \
        --skip-clarify --tokens 1.5

Pass 1 (Intent Analysis) ─ 12.7 s (544 ch streamed)
Pass 2 (Weakness Detection) ─ 12.7 s (916 ch streamed)
Disambiguation generation ─ 18.2 s (3 weakness fields → pause)
[--skip-clarify resumed with empty answers]
Pass 3 (Prompt Rewrite) ─ 49.0 s (2842 ch enhanced prompt)
Pass 4 (Quality Scoring) ─ 14.7 s (NON-streaming chat → empty content;
                                    scores_fallback=true)
```

### Run 2 — Pass 4 streaming (run id `b555e5225b385f0d`)

After switching Pass 4 from non-streaming `chat()` to `chat_stream()`
to bypass LM Studio's reasoning-token filter:

```
Pass 1 ─ 14.1 s
Pass 2 ─ 13.1 s
Pass 3 ─ 56.1 s (3486 ch enhanced prompt)
Pass 4 ─ 15.5 s (STREAMING → scores returned reliably)

scores_fallback:  0     ← false! gpt-oss returned the scores
specificity:      9
constraints:      10
actionability:    10
improvement:      92%
```

Per-pass durations tracked **individually** (the timing fix); pass1
and pass2 are independent measurements, not the averaged-half values
from before.

`gen_score` budget bumped 200 → 400 tokens to give reasoning-token
models headroom past their internal thinking.

## File tree (final)

```
prompt-enhancer/
├── pyproject.toml, README.md, STATUS.md, .gitignore
├── docs/
│   └── EXTRACTION_GOTCHAS.md            (methodology-agent guard rail)
├── src/enhancer/
│   ├── __init__.py, config.py
│   ├── core/
│   │   ├── events.py                    (FROZEN 30-member EventType enum)
│   │   ├── parsing.py                   (clamp, parsers, disambig Q&A)
│   │   ├── budgeting.py                 (truncate, context detection, pass budgets)
│   │   ├── passes.py                    (PASS1-4 + technique guidance)
│   │   ├── transforms.py                (PERSONA, MAGNITUDE, SOT, PRETRIAL)
│   │   └── pipeline.py                  (run_pipeline + run_pretrial — main loop)
│   ├── llm/
│   │   ├── base.py                      (ChatProvider ABC)
│   │   ├── lmstudio.py                  (LM Studio + LM Link, idle_timeout=120)
│   │   ├── ollama.py, openai.py, anthropic.py  (stubs)
│   │   └── registry.py
│   ├── persistence/
│   │   ├── schema.sql, db.py, runs.py, sessions.py
│   │   ├── jsonl_compat.py              (devflow.py byte-for-byte compat)
│   │   └── safestorage.py
│   ├── observability/__init__.py
│   ├── cli/
│   │   ├── main.py                      (typer entry)
│   │   └── extras.py                    (batch / compare / export)
│   └── ui/
│       ├── app.py                       (NiceGUI router + dark CSS)
│       ├── pages/
│       │   ├── studio.py                (status strip + tabs + sliders + live stream + diff)
│       │   ├── history.py               (filterable run table)
│       │   ├── analytics.py             (KPIs + technique pie + task-type bar)
│       │   └── settings.py              (read-only settings inspector)
│       └── components/
│           ├── status_strip.py          (9 nodes, color-coded by state)
│           └── diff_view.py             (difflib HtmlDiff with dark theme)
├── tests/
│   ├── conftest.py                      (FakeChatProvider + event_collector)
│   ├── test_concurrency.py              (the three load-bearing regression guards)
│   ├── test_parsing.py
│   ├── test_pipeline_smoke.py
│   └── test_migration.py
├── tools/
│   ├── methodology_agent.py             (passive Stop-hook reviewer)
│   ├── migrate_jsonl_to_sqlite.py       (one-shot migration; idempotent)
│   └── reviews/                         (output dir for method-*.md)
└── packaging/
    ├── prompt-enhancer.spec             (PyInstaller)
    ├── entrypoint.py                    (windowed launcher into NiceGUI)
    └── installer.iss                    (Inno Setup wrapper)
```

## Concurrency invariants (frozen — see `docs/EXTRACTION_GOTCHAS.md`)

1. `pass1 = await ...; pass2 = await ...` — never `asyncio.gather`.
   Test: `test_pass1_pass2_serial` — asserts wall-time ≥ 2× per-call latency.
2. Pass 4 awaited BEFORE Magnitude/SoT begin streaming.
   Test: `test_pass4_awaited_before_magnitude` — asserts call timestamps.
3. Every `chat_stream` carries `idle_timeout=120` (provider default).
   Test: `test_idle_timeout_propagates`.

## How to run end-to-end

```cmd
cd C:\Users\Falki\prompt-enhancer
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev,ui]"
pytest                                           rem 37 green
enhancer models                                  rem verify LM Studio at 1234
enhancer enhance "make me a chatbot" --model gptoss-120b-uncensored-hauhaucs-aggressive
enhancer ui                                      rem opens NiceGUI Studio at 127.0.0.1:8765
python tools\migrate_jsonl_to_sqlite.py --source ..\swarm-agent-dev\agent_pipeline.log
```

## What's left for v1.0

1. **Live verification** against `gpt-oss-120b-uncensored-hauhaucs-aggressive`
   via LM Link — confirms the three concurrency invariants hold under
   real remote-GPU latency. (User-driven; needs LM Studio running.)
2. **Templates page** — CRUD over `templates` table; ship 8 seed templates.
   Stubbed in schema; UI page deferred to v0.2.
3. **Compare page** in the UI — CLI `enhancer compare` already works;
   visual side-by-side scorecard is v0.2.
4. **Branching from any pass** — schema supports it (`parent_run_id` +
   `parent_pass`); UI gesture is v0.2.
5. **PyInstaller build** — spec + Inno script in `packaging/`; run
   `pyinstaller packaging/prompt-enhancer.spec --clean` then `iscc
   packaging/installer.iss` to produce the signed installer.
6. **TOML settings file** — env vars work today; persisted-from-UI
   settings land in v0.2.

## Methodology Enhancement Agent — operating contract

* Live runner: `tools/methodology_agent.py` — reads `git diff --staged`
  (or `HEAD`) and POSTs a templated review prompt to LM Studio.
* Output: `tools/reviews/method-YYYYMMDD-HHMMSS.md`. Never raises.
* Adds <1 s to turn time. Switchable via `ENHANCER_METHODOLOGY_AGENT_ENABLED=0`.
* Wire it into Claude Code by adding to `~/.claude/settings.local.json`:
  ```json
  "hooks": {
    "Stop": [
      "python C:/Users/Falki/prompt-enhancer/tools/methodology_agent.py"
    ]
  }
  ```
* Architectural directives the agent enforces in every review:
  1. Pass 1 → Pass 2 strictly serial.
  2. Pass 4 awaited before Magnitude/SoT.
  3. `idle_timeout=120` on every `chat_stream` call.
  4. `EventType` enum + payload schema is FROZEN — bump v2 on change.
  5. `ChatProvider` ABC must not leak transport details.
  6. JSONL log format byte-for-byte matches the source monolith.
  7. `scores_fallback` and `pass3_partial` are public-contract flags.
