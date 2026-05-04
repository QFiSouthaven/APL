# development

Local LLM-driven app builder — the 4th product in the APL umbrella, alongside `prompt-enhancer`, `round-robin`, and `hardware-info`.

## What it does

Drives a local LM Studio model through a staged "stack-app" build pipeline:

1. **Architect** (v0.1, end-to-end) — turns a goal into a structured JSON plan: stack, layers, dependencies.
2. **Coder** (v2.x) — emits per-layer source files.
3. **Reviewer** (v2.x) — static review of the Coder's output.
4. **Tester** (v2.x) — generates and runs a test suite.
5. **Packager** (v2.x) — produces a distributable artifact.

All stages publish events to a SQLite-backed message board so peer products (round-robin in particular) can subscribe to progress.

## Discovery

- Service key: `development`
- Default URL: `http://127.0.0.1:8767`
- Wired into `prompt_enhancer.api.discovery.DEFAULTS`, `round_robin.discovery.DEFAULTS`, and `APL/lab/`.

## Run

```bash
# from APL/development/
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m uvicorn development.server:app --host 127.0.0.1 --port 8767
```

## Endpoints

- `GET /api/health` — `{"status":"ok","service":"development","version":"..."}`
- `GET /api/peers`  — full discovery table
- `POST /api/build` — body: `{"goal": "..."}`, returns a `BuildResult`
- `GET /api/runs`   — terminal events from the message board
