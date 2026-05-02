# Providers — adding a new LLM backend

The standalone routes every LLM call through `enhancer.llm.base.ChatProvider`.
Implementing a new backend is mechanical; this doc walks through it.

## Contract

```python
from collections.abc import AsyncIterator
from typing import ClassVar
from enhancer.llm.base import ChatProvider


class MyProvider(ChatProvider):
    name: ClassVar[str] = "myprovider"

    async def list_models(self) -> list[str]:
        ...

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> str:
        ...

    async def chat_stream(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        idle_timeout: float = 120.0,
    ) -> AsyncIterator[str]:
        ...

    async def context_window(self, model: str) -> int | None:
        return None  # optional override
```

## Invariants every provider must satisfy

1. **`idle_timeout` is honored on `chat_stream`.** If the backend
   stalls for `idle_timeout` seconds without emitting a chunk, the call
   must raise (e.g., `httpx.ReadTimeout`). The pipeline's three
   concurrency invariants depend on this — see
   `docs/EXTRACTION_GOTCHAS.md` §3.

2. **Streaming yields user-visible content only.** Reasoning tokens,
   tool calls, system noise must be filtered or excluded. (Pass 4 and
   Persona switched to streaming specifically to bypass LM Studio's
   reasoning-token filter — your provider must produce a clean stream.)

3. **`temperature` and `max_tokens` propagate.** The pipeline forwards
   user-controlled values to every call; ignoring them silently
   breaks the temperature slider in the UI.

4. **`list_models()` is fresh.** No caching at the provider layer —
   the UI dropdown reflects whatever the backend currently exposes.
   Cache at the call-site if needed (the monolith's `model_router.py`
   does so with a 60-second TTL).

5. **Errors propagate.** Don't swallow `httpx.HTTPError` /
   `asyncio.TimeoutError`; the pipeline emits `agent_error` events
   based on these.

## Conformance test

Any provider should pass:

```python
@pytest.mark.asyncio
async def test_provider_conforms(my_provider):
    models = await my_provider.list_models()
    assert isinstance(models, list)

    text = await my_provider.chat(
        [{"role": "user", "content": "Reply: OK"}],
        model=models[0], max_tokens=50,
    )
    assert isinstance(text, str)

    chunks = []
    async for tok in my_provider.chat_stream(
        [{"role": "user", "content": "Reply: OK"}],
        model=models[0], max_tokens=50,
        idle_timeout=10.0,
    ):
        chunks.append(tok)
    assert "".join(chunks)
```

A more rigorous suite belongs in `tests/test_providers.py` (planned v0.2)
that exercises the same prompt set against multiple providers.

## Wiring

After implementing the class, register it in
`enhancer/llm/registry.py`:

```python
def get_provider(settings: Settings) -> ChatProvider:
    name = settings.provider.lower().strip()
    if name == "myprovider":
        from .myprovider import MyProvider
        return MyProvider(api_key=settings.myprovider_api_key, ...)
    ...
```

Add the optional dependency to `pyproject.toml`:

```toml
[project.optional-dependencies]
myprovider = ["myprovider-sdk>=1.0"]
```

so users install via `pip install prompt-enhancer[myprovider]`.

## Existing implementations

### LM Studio (v1)

`enhancer/llm/lmstudio.py` is the reference. It uses httpx + SSE + the
LM Studio management API (`/api/v0/models`) for `context_window`.

Notes:
- Two endpoints in play: `/v1/chat/completions` (inference) and
  `/api/v0/models` (mgmt). Don't confuse them.
- LM Link bridges remote machines into the same `/v1` surface.
- gpt-oss-family models emit reasoning tokens before visible content.
  Use streaming, not non-streaming chat. `gen_score` and `gen_persona`
  budgets default to 400 tokens to give reasoning models headroom.

### Ollama (stub)

`enhancer/llm/ollama.py` raises `NotImplementedError` with an install
hint. Ollama exposes an OpenAI-compatible endpoint at
`localhost:11434/v1/chat/completions` so the implementation is almost
identical to `LMStudioProvider` — port the streaming logic + override
`list_models()` to call `/api/tags` instead.

### OpenAI (stub) / Anthropic (stub)

Both stubs raise with helpful messages. v0.2 will use the official
SDKs. For Anthropic, prefer LM Studio's `/v1/messages` Anthropic-compat
endpoint as the fastest path to ship.

## Adding context-window detection

If your backend exposes loaded context length, override
`context_window`. The pipeline calls this once at startup to size the
per-pass budgets via `core.budgeting.compute_pass_budgets`. Returning
`None` is fine — the budgeting layer falls back to model-name regex
patterns and parameter-count heuristics.

## Testing your provider live

```bash
ENHANCER_PROVIDER=myprovider \
ENHANCER_DEFAULT_MODEL=my-model \
enhancer enhance "Make me a chatbot" --skip-clarify
```

Watch for:
- `scores_fallback: 0` in the persisted run (Pass 4 returned scores).
- `pass3_partial: 0` (Pass 3 streamed normally).
- Reasonable `pass_times_ms` distribution.
- No `httpx.ReadTimeout` on the idle window.

If `scores_fallback=1` but Pass 1/2/3 succeeded, your provider's
non-streaming `chat()` likely has the same issue gpt-oss does — switch
the affected passes to streaming.
