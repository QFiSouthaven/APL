# UI Testing — v2.0 MVP

## Decision: import-only smoke tests (no harness)

We considered two options for testing the 12 NiceGUI modules in
`src/enhancer/ui/pages/` and `src/enhancer/ui/components/`:

1. `nicegui.testing` — official in-tree harness (`User` / `Screen` fixtures).
2. Import-only smoke: import the module, assert `render()` is callable,
   call it inside a swappable temp-DB context, exercise pure helpers.

**We picked option 2** for the v2.0 MVP. The reason is pragmatic: NiceGUI
permits component construction and `render()` invocation without a live
`@ui.page` context (auto slot), so the cheapest possible smoke — "import,
construct, call helpers" — already gives us a real signal: every UI
module loads, every entry point executes, every public/pure helper
returns the value its caller expects, and a regression that breaks any
of those will fail the test. Spinning up `nicegui.testing.User` adds
async fixtures, lifecycle, and FastAPI startup for what is essentially
a syntax + import check at the v2.0 stage.

We pin the user's real `data_dir` / `config_dir` to a `tmp_path` via
`monkeypatch` so seeded templates and session writes during a render
call don't pollute `%APPDATA%\\prompt-enhancer\\enhancer.db`.

## What's covered today (v2.0)

- All 6 page modules import.
- All 6 page `render()` functions are callable AND execute end-to-end
  against a temp database without raising.
- All 6 component modules import.
- Public render entry points (`render_diff`, `render_score_chips`,
  `render_pass_card`, `render_branch_tree`) execute without raising.
- `StatusStrip` constructs and `set` / `reset` flip node state.
- `SessionDrawer` constructs against a temp DB and `session_context_for`
  returns an empty string for a missing session id.
- Pure helpers (`_format_clock`, `_band_for`, `_fmt_duration`,
  `_truncate_model`, `_label_for`, `_load_lineage`, `_fetch_rows`,
  `_seed_if_empty`) are exercised across at least one branch each.

## What's deferred to v2.1

- Real interaction tests via `nicegui.testing.User` — clicking the
  Enhance button, typing into the prompt textarea, asserting that the
  status strip transitions through `running → done`, asserting that
  the disambiguation modal opens.
- Playwright end-to-end coverage — DOM-level assertions, screenshot
  diffs, real keyboard-driven flows.
- Drag/keyboard interaction on the History table row click.
- Studio's `_run_pipeline_action` against a `FakeChatProvider` —
  needs the harness to drive the click + an `asyncio` runtime.

The v2.1 work is tracked in STATUS.md; the harness selection at that
point can revisit `nicegui.testing` vs Playwright with a concrete
interaction list in hand.
