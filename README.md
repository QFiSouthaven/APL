# APL — workspace umbrella

This repo is the umbrella for a small constellation of local-first
LLM tools. Each component lives in its own subdirectory with its own
`pyproject.toml`, `.venv/`, tests, and release cycle. The umbrella's
job is shared service discovery (so the components can find and call
each other) and orchestrated boot (`lab/launch.py`).

## Components

| Path | What it is | Default port |
|------|-----------|-------------|
| `prompt-enhancer/` | Multi-pass AI prompt enhancer with NiceGUI desktop studio. LM Studio first, providers pluggable (Ollama, OpenAI, Anthropic). | `8765` |
| `round-robin/` | Two-LLM dialogue desktop app over LM Studio LM Link, with optional Charlie sandbox implementer. | `8766` |
| `right-pipe/` | (Reserved — empty placeholder.) | `8767` |
| `hardware-info/` | Hardware-state archive for machine `m5`. Not a service. | n/a |

Each component is independently runnable. The umbrella adds:

- **Shared service discovery** — every component reads
  `%APPDATA%\swarm\services.toml` (or `~/.config/swarm/services.toml`)
  on startup to look up sibling URLs. Defaults baked into each
  component match the table above, so the file is optional unless
  ports are remapped.
- **Coordinated launcher** — `lab/launch.py` boots each declared
  component in dependency order, blocks until each `/api/health`
  returns 200, and shuts everything down cleanly on Ctrl-C.

## Quick start

From the umbrella root (`C:\Users\Falki\APL`):

```bash
# 1. (Once per machine) seed the discovery file
python lab/onboarding.py

# 2. Boot prompt-enhancer + round-robin together
python lab/launch.py

# Or boot just one:
python lab/launch.py prompt-enhancer
```

Each component's own README in its subdirectory covers the inner
workings. This file stays at the umbrella level only.

## Cross-component contract

The two introspection endpoints are stable across components:

```
GET /api/health  -> {"status":"ok","service":"<name>","version":"<x.y.z>"}
GET /api/peers   -> {"services": {"<name>": "<url>", ...}}
```

prompt-enhancer also exposes a forward-to endpoint for one-step peer
invocation:

```
POST /api/forward-to/{peer}  body: <peer's enhance request>
                              -> peer's response body verbatim
```

## Adding a new component

1. Create the component directory at the umbrella root.
2. Reserve a port in `lab/onboarding.py`'s `DEFAULTS` (and add it to
   the table above).
3. Implement a `discovery` module (mirror
   `prompt-enhancer/src/enhancer/api/discovery.py` or
   `round-robin/src/round_robin/discovery.py`).
4. Expose `/api/health` and `/api/peers` matching the contract.
5. Add an entry to `lab/launch.py` `COMPONENTS` dict.

## Repo

Hosted at `https://github.com/QFiSouthaven/APL`. Each tagged release
is the prompt-enhancer release; sibling components currently piggy-back
on the umbrella version. That may split in v2.x if components diverge
in cadence.
