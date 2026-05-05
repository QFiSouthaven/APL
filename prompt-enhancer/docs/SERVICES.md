# services.toml — cross-sibling discovery

The four-product APL loop (Prompt Enhancer, Round Robin, Interpreter,
Loop Driver) finds peers through a single shared TOML file. Each
product reads it on demand; nothing caches and nothing imports across
product boundaries. If the file is absent the products fall back to
localhost loopback ports so dev still works out of the box.

## Where it lives

| Platform     | Path                                          |
| ------------ | --------------------------------------------- |
| Windows      | `%APPDATA%\swarm\services.toml`               |
| Linux/macOS  | `~/.config/swarm/services.toml`               |

The path is computed by `platformdirs.user_config_dir("swarm",
appauthor=False)`. To see the exact location on your machine:

```bash
enhancer services path
```

## Schema

A single `[services]` table mapping peer name to base URL.

```toml
[services]
prompt_enhancer = "http://127.0.0.1:8765"
round_robin     = "http://127.0.0.1:8766"
development     = "http://127.0.0.1:8767"
```

Trailing slashes are stripped on read. Unknown keys are passed through
unchanged — the loop tolerates extra peers it doesn't know about.

## Precedence

1. The `[services]` table in this file (if it parses).
2. `enhancer.api.discovery.DEFAULTS` (the loopback ports above).
3. The optional `default=` argument to `get_peer_url(name, default=...)`.

If the file is absent, malformed, or unreadable, the loader silently
falls back to `DEFAULTS` so a broken config never crashes startup.

## Peer name conventions

Peer names use **snake_case**: `round_robin`, NOT `round-robin`. This
matches the fix in commit f0389be — kebab-case names will not match
the loader's lookup table.

Known peers today:

| Name              | What it is                                  |
| ----------------- | ------------------------------------------- |
| `prompt_enhancer` | This product's REST API and Studio UI       |
| `round_robin`     | The round-robin sibling in the APL umbrella |
| `development`     | Interpreter / dev sandbox peer              |

## Bootstrapping

The CLI ships three subcommands for managing this file:

```bash
enhancer services show       # print resolved peer table + file status
enhancer services init       # write a starter services.toml
enhancer services init --force   # overwrite an existing file
enhancer services path       # print just the absolute path
```

`init` writes a file with the DEFAULTS table inlined (so it parses to
a working `[services]` block even if you uncomment nothing) plus a
header comment block summarizing this page and a commented-out LAN
override example at the bottom.

## Troubleshooting

- **Override not picked up?** Run `enhancer services show` — if it
  still shows the default URL, the file likely failed to parse and the
  loader silently fell back. Check for unmatched quotes or stray
  characters.
- **Wrong path?** `enhancer services path` is authoritative — that is
  the path every sibling reads. If you edited a file elsewhere, move
  it here.
- **Need to start over?** `enhancer services init --force` will
  overwrite the file with a fresh starter.
