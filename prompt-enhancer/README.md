# prompt-enhancer

> Local Desktop Studio for multi-pass AI prompt enhancement.
> LM Studio first; Ollama / OpenAI / Anthropic pluggable.

Extracted from
[swarm-agent-dev](https://github.com/halkive/swarm-agent-dev) Agent Loop
mod into a single distributable product.

## What it does

A 4-pass enhancer that turns rough prompts into production prompts:

```
Intent analysis  →  Weakness detection  →  Task-aware rewrite  →  Quality score
                          (optional)        (with self-correction retry)
                          interactive
                          disambiguation
```

Plus optional **Persona**, **Magnitude blueprint**, and
**Skeleton-of-Thought** transforms.

## Quick start

```bash
pipx install prompt-enhancer
enhancer enhance "make me a chatbot"      # CLI, streamed
enhancer ui                                # Desktop Studio (NiceGUI)
```

For non-Python users: `prompt-enhancer-setup.exe` from
[Releases](https://github.com/halkive/prompt-enhancer/releases).

## Build phases

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target shape
and [docs/EXTRACTION_GOTCHAS.md](docs/EXTRACTION_GOTCHAS.md) for the
guard rail the implementation reads against.

| Phase | Status |
|---|---|
| 0 — scaffold | in progress |
| 1 — extract core | pending |
| 2 — SQLite persistence | pending |
| 3 — ChatProvider abstraction | pending |
| 4 — typer CLI | pending |
| 5 — Desktop Studio UI | pending |
| 6 — packaging | pending |
| 7 — verification & polish | pending |

## License

MIT.
