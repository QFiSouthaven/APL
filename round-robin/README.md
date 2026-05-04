# Round Robin

Two-LLM dialogue desktop app. Alpha runs on this machine, Bravo runs on another machine, both connected through **LM Studio's LM Link** mesh. Optional third agent (Charlie) implements files in a sandbox when the dialogue says "Confirmed".

## Setup

1. Install LM Studio on both machines, enable LM Link, sign in with the same account, and confirm `lms link status` shows the pair.
2. Load a model in each LM Studio instance.
3. On the Alpha machine:

```
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python app.py
```

A native window opens. Pick a model for each agent (the dropdown lists models on both machines), set a theme, hit Start.

## Test

```
pytest -v
```

## Layout

```
src/round_robin/
  config.py         paths and defaults
  storage.py        atomic JSON persistence with .bak recovery
  lm_client.py      single httpx client to localhost:1234 (LM Link routes to Bravo)
  health.py         /v1/models probe + optional `lms link status`
  lms_cli.py        optional subprocess wrapper for the lms CLI
  orchestrator.py   turn loop, pause/resume, retry/skip
  sessions.py       PresetStore + SessionStore
  server.py         FastAPI REST + /ws
  charlie/          sandbox + implementer agent
  static/           HTML/CSS/JS frontend
app.py              launches uvicorn in background + opens pywebview window
```

## LM Link note

With LM Link enabled, your client always talks to `http://localhost:1234/v1`. LM Studio routes the request to the remote machine based on the requested `model` identifier. There is no per-agent host URL in the UI — pick the model that lives on the machine you want.
