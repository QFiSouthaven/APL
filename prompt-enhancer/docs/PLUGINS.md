# Plugins — extension points

The standalone exposes three layers of extension:

1. **Provider plugins** — new LLM backends.
2. **Pass plugins** — new transform passes after the core 4.
3. **CLI / UI commands** — new user-facing surfaces.

This doc covers all three.

---

## 1. Provider plugins

See [`PROVIDERS.md`](PROVIDERS.md) for the contract and conformance test.

### Entry-point group (planned v0.2)

```toml
# In your plugin package's pyproject.toml:
[project.entry-points."enhancer.providers"]
myprovider = "my_pkg.provider:MyProvider"
```

The runtime discovery in `enhancer.llm.registry` will iterate
`importlib.metadata.entry_points(group="enhancer.providers")` and call
`get_provider()` against the matching name.

For v0.1, providers are hardcoded in
`enhancer/llm/registry.py::get_provider`. Add a new branch for your
backend.

---

## 2. Pass plugins

The 4-pass core is intentionally fixed (Intent / Weakness / Rewrite /
Score). You can add **side-channel transforms** that run after Pass 4
the same way Magnitude and Skeleton-of-Thought do.

### Anatomy of a transform

A transform is:
- A constant system prompt (in `core/transforms.py`).
- A toggle in `PipelineOptions` (e.g., `magnitude_mode: bool`).
- A streaming block in `run_pipeline()` after the Pass 4 await.
- A pair of `EventType` members (`MY_START`, `MY_CHUNK`, `MY_DONE`,
  `MY_ERROR`).

### Steps to add one

1. **Define the system prompt** in `core/transforms.py`:

   ```python
   MY_TRANSFORM_SYSTEM = "You are a foo. Do X. Output Y."
   ```

2. **Add events** to `core/events.py`:

   ```python
   class EventType(str, Enum):
       ...
       MY_START = "my_start"
       MY_CHUNK = "my_chunk"
       MY_DONE = "my_done"
       MY_ERROR = "my_error"
   ```

3. **Add the toggle** to `PipelineOptions` in `core/pipeline.py`:

   ```python
   @dataclass
   class PipelineOptions:
       ...
       my_transform_mode: bool = False
   ```

4. **Add the streaming block** after the existing Magnitude / SoT blocks
   in `run_pipeline`:

   ```python
   my_output = ""
   if opts.my_transform_mode:
       await _emit(on_event, EventType.MY_START)
       chunks = []
       try:
           async for tok in provider.chat_stream(
               messages=[
                   {"role": "system", "content": MY_TRANSFORM_SYSTEM},
                   {"role": "user", "content": truncate(enhanced, char_budget, "my")},
               ],
               model=model, temperature=temperature,
               max_tokens=scaled(budgets.<your_budget>, max_tokens_scale),
               timeout=request_timeout, idle_timeout=idle_timeout,
           ):
               chunks.append(tok)
               await _emit(on_event, EventType.MY_CHUNK, token=tok)
       except Exception as exc:
           await _emit(on_event, EventType.MY_ERROR, error=str(exc))
       my_output = "".join(chunks)
       await _emit(on_event, EventType.MY_DONE, content=my_output)
   ```

5. **Add the budget** to `core/budgeting.py::PassBudgets` and
   `compute_pass_budgets()`.

6. **Persist the output** by adding a column to `persistence/schema.sql`
   and the corresponding field to `persistence.runs.RunRecord`.

7. **Wire the UI** — add a tab or panel in `ui/pages/studio.py` that
   listens for the new chunk events.

8. **Document** in `EVENTS.md` and add a regression test.

### Ordering rules

- Transforms run **after** Pass 4 has been awaited. Single-instance
  backends serialize requests; running a transform stream alongside
  Pass 4 deadlocks.
- Multiple transforms run **serially** for the same reason.
- Each transform should respect `temperature` and `max_tokens_scale`
  the same way the core passes do.

---

## 3. CLI / UI commands

### Adding a CLI command

In `enhancer/cli/extras.py`:

```python
def my_command(arg: str = typer.Argument(...)):
    """Short description."""
    settings = load()
    provider = get_provider(settings)
    # ... your work ...
    console.print("done")


def register(app: typer.Typer) -> None:
    app.command()(my_command)
    # plus existing commands
```

Then `enhancer my-command "..."` works.

### Adding a UI page

1. Create `enhancer/ui/pages/my_page.py` with a `render()` function
   that calls a sidebar helper and lays out the page using NiceGUI
   primitives.
2. Add a route in `enhancer/ui/app.py`:

   ```python
   from .pages import my_page

   @ui.page("/my-path")
   def _my():
       _inject_dark_styles()
       my_page.render()
   ```

3. Add a sidebar link in every page's `_sidebar()` helper.

For a UI component reused across pages, drop it in
`enhancer/ui/components/`.

---

## 4. Configuration extension

To add a new env-driven setting:

1. Add a field to `Settings` in `enhancer/config.py`.
2. Add a corresponding env var to the `load()` function.
3. Document in `README.md` or here.

Example:

```python
@dataclass(frozen=True)
class Settings:
    ...
    my_feature_enabled: bool = False
```

Set via `set ENHANCER_MY_FEATURE_ENABLED=1`. v0.2 will gain a TOML
overlay (see `MIGRATION.md` § "Settings migration").

---

## 5. Hooks and lifecycle integrations

### Methodology Enhancement Agent

`tools/methodology_agent.py` is invoked from a Claude Code `Stop`
hook. Wire it by adding to `~/.claude/settings.local.json`:

```json
{
  "hooks": {
    "Stop": [
      "python C:/Users/Falki/prompt-enhancer/tools/methodology_agent.py"
    ]
  }
}
```

The script reads `git diff --staged` (or `HEAD`), POSTs a templated
review prompt to LM Studio, and writes
`tools/reviews/method-YYYYMMDD-HHMMSS.md`. Never raises. Switchable via
`ENHANCER_METHODOLOGY_AGENT_ENABLED=0`.

### MCP tools (planned v0.2)

LM Studio supports MCP servers via `mcp.json`. The standalone will
gain a `chat_with_tools` method on `ChatProvider` that lets passes
optionally invoke registered MCP tools. See
`~/.claude/knowledge/lm-studio-mcp-integration.md` and
`~/.claude/knowledge/helplms-mcp-server.md` for the design.

---

## 6. Roadmap stubs

Things the architecture is ready for but doesn't yet implement:

- **Branching gesture in the History UI** — schema supports it
  (`parent_run_id` / `parent_pass`); UI gesture missing.
- **Compare across two providers** (not just two models on one
  provider) — `compare` CLI accepts arbitrary models; the UI Compare
  page should grow a provider dropdown.
- **Export run as a Jupyter notebook** — `enhancer export <run_id>
  --format ipynb` would unlock the analytical task type as a
  worksheet workflow.
- **Templates → workspace** — connect templates to a default
  scorer-model preference per template.
