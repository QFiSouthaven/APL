# Extraction Gotcha Map

> Authored by the Methodology Enhancement Agent (parallel pass). This is
> the guard rail the implementation thread reads against while porting
> `agent_pipeline.py` (~1664 lines) and `lmstudio.py` (~202 lines) from
> the swarm-agent-dev monolith.

**Source:** `C:\Users\Falki\swarm-agent-dev\src\webui\mods\agent_pipeline.py`
and `C:\Users\Falki\swarm-agent-dev\src\webui\services\lmstudio.py`.

---

## 1. Hidden coupling sites

### WebSocket / FastAPI binding
- `agent_pipeline.py:526–572` — `handle(websocket: WebSocket, message_data: dict)`
  becomes `async def run_pipeline(prompt, opts, on_event=None)` where
  `on_event: Callable[[EventType, dict], Awaitable[None]] | None`.
- `agent_pipeline.py:896–914` — `_ws_alive()` + `send_step()` closure check
  `websocket.client_state == WebSocketState.CONNECTED` and call
  `websocket.send_text(json.dumps(...))`. Replace with `on_event(...)`.
  The `await asyncio.sleep(0.05)` flush at line 914 is **kept verbatim**
  (was 0.3, dropped to 0.05 — saves ~3 s per run).

### emit() call sites (~40+)
Every `await self.emit(ws, "...", ...)` becomes
`await on_event(EventType.X, **payload)`. Notable ranges:
- Sessions: 642–647, 662, 677–682, 702, 708, 724, 731–738.
- Pretrial: 799, 809–814, 828, 858–866, 870.
- Pipeline backbone: 949–952, 1006–1013, 1046–1059, 1121–1125,
  1167–1189, 1256–1263, 1277–1280, 1308–1316, 1365–1370, 1399–1405,
  1422–1437, 1449, 1461–1462, 1473–1475, 1482, 1493–1495, 1503–1505,
  1537–1543, 1555–1558, 1566–1572, 1596–1609, 1611–1634, 1650–1662.

The standalone's `EventType` enum + payload schema in `core/events.py`
is the **frozen API boundary** — changing it breaks `devflow.py` and
`chain_events.py` consumers in the monolith.

### Instance state — `self._pending_disambig`
- `agent_pipeline.py:524` — `dict[str, dict]` storing in-flight
  disambiguation state, keyed by `disambig_id` (line 1101).
- Risk: NiceGUI may serve concurrent requests; wrap accesses in an
  `asyncio.Lock`. The `disambig_id` is a hex token so collisions are
  effectively impossible — no rename needed.

---

## 2. Order-dependent state mutations (mines)

### Timing variables
- Pass 1/2: `t0` set at 1037, `pass1`/`pass2` awaited 1038–1039, `t1` at
  1040, `t2 = t1` at 1041.
- `t2_effective = tp1 if persona_mode and persona_time_ms else t2`
  (1207–1210). **Hidden risk:** if `persona_mode=True` but the persona
  call errors (1171–1176), `tp1` is undefined → `NameError`. Add a
  `tp1 = t2` initializer in the standalone before the try.

### `enhanced_chunks` accumulator
- 1265–1305 — Pass 3 streams into `enhanced_chunks: list[str]`. On
  stream error: `pass3_partial = True` (1283); fallback appends
  original `prompt` (1286). `enhanced = "".join(...)` at 1305.
- **Intentional behavior, not a bug:** if 100 tokens stream in then
  fail, `enhanced` is `"100 tokens" + "original prompt"`. Document.

### `pass3_partial` flag (set 2 places, read 3)
- Initial `False` at 1266 → set `True` at 1283 → read at 1514
  (skip retry) and 1362–1363 (skip Pass 4 if `enhanced == prompt`).
- **Lint rule:** no code mutating `enhanced` between 1283 and 1362.

### `scores_fallback` flag
- Initial `False` at 1361 → may set `True` at 1363 → may overwrite
  at 1417 (`= not bool(pass4)`) → read at 1430, 1616, 1631.
- **Stale-by-design:** if Pass 4 was never fired, line 1417 never
  runs; the value from 1363 survives. That is correct.

### `pass4_task` lifecycle
- Created at 1371 (`asyncio.create_task(_run_pass4_bg())`); awaited at
  1415–1418; **`pass4_task = None  # mark as already consumed` at 1418
  is load-bearing.** Removing it lets later code re-await an exhausted
  task → hang.

### `_disambig_ctx` / `_resume_state`
- 956–975 — on disambiguation resume, unpack `_resume_state` into
  `pass1, pass2, task_type, technique, session_context, _disambig_ctx,
  t0, t1`; set `t2 = t1`.
- 1239–1240 — `_disambig_ctx` (possibly empty string) appended to
  Pass 3 user message.
- Add `assert _resume_state.get("pass1") is not None` if
  `_resume_state` provided — guard against partial fills.

---

## 3. Concurrency invariants — the three lessons

### Invariant 1: Pass 1 → Pass 2 strictly serial (1036–1040)
```python
pass1 = await _run_pass1()
pass2 = await _run_pass2()
```
Comment at 1035–1036 explains why. **Regression test:**
`test_pass1_pass2_serial` — fake provider with 0.5s latency, assert wall
time ≥ 1.0 s.

### Invariant 2: Pass 4 awaited BEFORE Magnitude/SoT (1409–1421)
```python
if pass4_task is not None:
    pass4, scores, t4 = await pass4_task
    scores_fallback = not bool(pass4)
    pass4_task = None
else:
    scores = _p4_defaults
```
**Regression test:** `test_pass4_awaited_before_magnitude` — mock
provider logs call timestamps, assert Pass 4 ends before first
Magnitude call begins.

### Invariant 3: Idle timeout on every stream
- `lmstudio.py:137` — `idle_timeout: float = 120.0` (default).
- `lmstudio.py:177–179` — `httpx.Timeout(timeout, connect=15.0,
  read=idle_timeout)`.
- All call sites (1017, 1027, 1452, 1485) rely on the default.
  **Do NOT change the default.**
- **Regression test:** `test_idle_timeout_fires_on_silent_stall` —
  fake provider yields 1 token then sleeps 200 s; assert
  `httpx.ReadTimeout` within 130 s.

---

## 4. Parsing quirks

### `_parse_task_type()` (383–401)
Maps noisy LLM output to `{creative, analytical, factual,
instructional, conversational}`. Special post-processing at 1132–1136:
if `task_type == "instructional"` and prompt contains code keywords
(`code`, `function`, `api`, `class`, `implement`), override to
`"coding"`. Document this — it's outside the parser.

### `_parse_technique()` (368–375)
Default `"precision"` if missing/invalid. Lowercase membership test in
`{precision, context, structure}`.

### `_parse_persona()` (404–409)
Returns raw `PERSONA:` line. Empty fallback handled at 1181:
`"world-class prompt engineer"`.

### `_parse_scores()` (412–428)
Defaults: `{specificity:5, constraints:5, actionability:5,
improvement:50}`. Per-key search line-by-line; first space-separated
integer after the colon. **Last occurrence wins** (no early break) —
preserve this behavior verbatim, fix only if tests demand it.

### `_parse_disambiguate_questions()` (496–513)
- Q lines: `Q1:` / `Q2:` / `Q3:`.
- Option lines: `A)` / `B)` / `C)` — single-character + `)`. **Won't
  parse `10)`** — acceptable, max 2–3 options per question.

### `_count_weakness_fields()` (482–493)
Counts non-empty, non-`none`/`n/a`/`none found` values across
`VAGUE TERMS`, `MISSING CONTEXT`, `UNSTATED CONSTRAINTS`,
`SCOPE ISSUES`. Triggers disambiguation when ≥ 3 (1072,
`_DISAMBIGUATE_THRESHOLD`).

---

## 5. Budget arithmetic (token formulas)

`agent_pipeline.py:920–927`:
```python
tok = budget // 4
gen_analysis  = max(tok // 8, 512)
gen_rewrite   = max(tok // 2, 2048)
gen_score     = 200
gen_persona   = 200
gen_magnitude = max(tok // 2, 4096)
gen_sot       = max(tok // 3, 2048)
```
`budget` (chars) from `_detect_context_budget()` at 918. Replicate
verbatim. Any change shifts output length distribution and breaks
analytics filters.

---

## 6. Smart truncation rule

`agent_pipeline.py:131–145` — first 20% + marker + last 80%. Marker
size dynamic; if `usable < 40` after marker, hard-chop. Applied at:
979/984–985 (P1), 999 (P2), 1151–1153 (Persona), 1221–1229 (P3),
1242–1245 (P3 session ctx), 1336–1337 (P4), 1447–1448 (Magnitude),
1480 (SoT).

---

## 7. Session context injection

`_build_session_context()` (582–626): newest-first, full enhanced
prompt for most-recent entry, 300-char summaries for older ones.
Default 3000 token budget (12 000 chars).

Pass 1 wrap (982–991) and Pass 3 wrap (1241–1251) both use
`[SESSION CONTEXT] ... [END SESSION CONTEXT]` markers, both budget
`= context_budget // 2`.

---

## 8. JSONL log schema (frozen API)

`_log_pipeline_run()` at 431–479 writes
`PROJECT_ROOT / "agent_pipeline.log"`.

**Always present:** `ts, prompt, technique, intent_preview,
weakness_preview, enhanced_preview`.

**Conditional:** `task_type, scores, pass_times_ms, scorer_model,
model, pass1_output, pass2_output, pass4_output, persona`.

The standalone `persistence/jsonl_compat.py` must produce **byte-for-
byte compatible** output so `devflow.py:56–72` keeps working.

---

## 9. Error-path fallbacks

| Trigger | Fallback | Flag set | Site |
|---|---|---|---|
| Pass 3 stream errors with partial output | use partial | `pass3_partial=True` | 1281–1304 |
| Pass 3 stream errors with no output | use original prompt | `pass3_partial=True` | 1284–1286 |
| Pass 4 errors | `_p4_defaults` | `scores_fallback=True` | 1354–1356, 1417 |
| Persona errors / empty parse | `"world-class prompt engineer"` | — | 1171–1181 |
| Magnitude stream errors | empty output, emit error event | — | 1464–1471 |
| SoT stream errors | empty output, emit error event | — | 1496–1501 |
| Disambig generation errors | `questions = []`, skip pause | — | 1096–1100 |
| Session save errors | log warning, continue | — | 1406–1407 |

---

## 10. Recently-fixed landmines (must not regress)

1. **Pass 1/2 serialization** — 1036–1040 (no `asyncio.gather`).
2. **Pass 4 awaited before Magnitude/SoT** — 1409–1421 (then 1439–1505).
3. **idle_timeout=120 on every stream** — `lmstudio.py:137`, used at
   1017, 1027, 1452, 1485.

---

## Top 7 implementation directives

1. Replace `self.emit(ws, …)` with `await on_event(EventType.X,
   **payload)`. Freeze the `EventType` enum + payload schema in
   `core/events.py` — that is the standalone's API boundary.
2. Serial Pass 1 → Pass 2: never gather, never parallelize.
   Regression test required.
3. Await Pass 4 BEFORE Magnitude/SoT — no exceptions. Comment
   1410–1412 carried verbatim. Regression test required.
4. Preserve `idle_timeout=120` on every `chat_stream` call. Regression
   test fires within 130 s on a 200 s stall.
5. Freeze the order of mutations for `t0/t1/t2/t2_effective/t3/t4`.
   Re-derive all `pass_times_ms` math by hand after refactor.
6. Carry `scores_fallback` and `pass3_partial` semantics unchanged.
   They are the contract with `devflow.py` and the analytics dashboard.
7. Dual-write JSONL for one release. `runs.save()` calls
   `db.insert_run()` AND `jsonl_compat.append()`. Schema byte-for-byte
   matches `_log_pipeline_run()` 449–456.
