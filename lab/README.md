# lab/

Cross-umbrella orchestration for the APL constellation.

## launch.py

Cross-umbrella orchestrator that boots all 3 sibling APIs and waits
for `/api/health` on each.

### Run

```bash
python lab/launch.py             # boot every component in COMPONENTS
python lab/launch.py --check     # dry-run; validate config, do not spawn
python lab/launch.py prompt_enhancer round_robin   # subset
```

### Behavior

- Reads `services.toml` (`%APPDATA%\swarm\services.toml` on Windows,
  `~/.config/swarm/services.toml` elsewhere). Falls back to the
  `DEFAULT_URLS` table baked into `launch.py` if the file is absent —
  fresh machines work without onboarding.
- Spawns each component as a subprocess in dependency-free order using
  the component's own `.venv/Scripts/python.exe` (Windows) so PATH
  activation isn't required.
- Polls each component's `/api/health` every 0.5s until 200, with a
  30s timeout per component. A failed sibling does not block boot of
  healthy peers — the launcher reports it and continues.
- Prints two banners per healthy component: `[launch] <name>: HEALTHY
  (<url>)` for parsing, and `[ok] <component> at <base-url>` for
  humans.
- On Ctrl-C: sends `terminate()` (Windows-friendly) to each child,
  waits up to 10s, then kills any stragglers. Exit code 0 on clean
  shutdown, 1 if no components could boot.

### Troubleshooting

- **Component listed as MISSING in `--check`**: the component's `.venv`
  hasn't been created. From that component's directory:
  `python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`
- **Component fails health within 30s**: its stderr is inherited by the
  launcher's terminal, so the underlying error (port conflict, missing
  dep, LM Studio unreachable) is visible inline. Re-run with the
  component's own command from its directory to reproduce in isolation.
- **`--check` reports `(unresolved)` for a base URL**: the component
  name isn't in `services.toml` AND not in `DEFAULT_URLS` — extend
  one or the other.

## onboarding.py

Seeds `services.toml` with the umbrella defaults. Run once per machine,
before the first `lab/launch.py` if you want to remap any port:

```bash
python lab/onboarding.py
```

Idempotent — refuses to overwrite an existing file.
