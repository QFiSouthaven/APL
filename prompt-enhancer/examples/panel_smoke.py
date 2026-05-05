"""End-to-end ReasoningPanel smoke against a live LM Studio.

Probes the local LM Studio (default ``http://localhost:1234``) for chat-
capable loaded models, builds a ReasoningPanel from the first 2-3 of
them, runs ``run_pipeline`` in ``parallel`` / ``primary-wins`` mode, and
pretty-prints + archives the panel telemetry that lands in
``PipelineResult.extras["panel"]``.

Run with::

    python examples/panel_smoke.py

Requires LM Studio reachable at the default management URL with at
least 2 chat-capable models loaded (``llm`` or ``vlm`` type, state
``loaded``). Load with the LM Studio desktop UI or::

    lms load <model-id-1>
    lms load <model-id-2>

Output: human-readable summary to stdout + a JSON trace dropped at
``tools/reviews/panel-smoke-YYYYMMDD-HHMMSS.json``. The JSON shape mirrors
``extras["panel"]`` exactly so downstream tooling can replay it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Add src to path so this is runnable from a clone without `pip install -e`.
THIS = Path(__file__).resolve()
ROOT = THIS.parent.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from enhancer.core.events import EventType  # noqa: E402
from enhancer.core.pipeline import (  # noqa: E402
    PipelineOptions,
    build_resume_state,
    run_pipeline,
)
from enhancer.llm.lms_discovery import discover_chat_models  # noqa: E402
from enhancer.llm.lmstudio import LMStudioProvider  # noqa: E402
from enhancer.llm.reasoning_panel import LLMSlot, ReasoningPanel  # noqa: E402

LMS_MGMT_URL = "http://localhost:1234"
LMS_BASE_URL = "http://localhost:1234/v1"
PROMPT = (
    "Write a concise commit message for adding rate limiting "
    "to a REST endpoint."
)
ARCHIVE_DIR = ROOT / "tools" / "reviews"


def _section(title: str) -> None:
    bar = "-" * 72
    print(f"\n{bar}\n{title}\n{bar}")


async def main() -> int:
    _section("ReasoningPanel smoke against live LM Studio")
    print(f"mgmt URL: {LMS_MGMT_URL}")
    print(f"prompt:   {PROMPT!r}")

    # 1. Discover loaded chat models.
    models = await discover_chat_models(LMS_MGMT_URL)
    loaded = [m for m in models if m.is_loaded]
    if len(loaded) < 2:
        print(
            "\nERROR: panel_smoke requires at least 2 chat-capable models "
            f"loaded in LM Studio (found {len(loaded)}). "
            "Load two models -- e.g. via LM Studio desktop UI or:\n"
            "    lms load <model-id-1>\n"
            "    lms load <model-id-2>\n"
            "then re-run this script. Loaded right now: "
            f"{[m.id for m in loaded] or '(none)'}"
        )
        return 1

    pick = loaded[: min(3, len(loaded))]
    print("\nloaded models picked for panel:")
    for i, m in enumerate(pick):
        role = "primary" if i == 0 else f"partner-{i}"
        print(f"  [{role:9s}] {m.id}  (type={m.type})")

    # 2. Build panel. One LMStudioProvider instance is shared across slots
    # -- they all hit the same local LM Studio; slot.model is what
    # differentiates them at the /chat/completions level.
    provider = LMStudioProvider(
        base_url=LMS_BASE_URL, management_url=LMS_MGMT_URL
    )
    slots = [LLMSlot(name="primary", provider=provider, model=pick[0].id)]
    for i, m in enumerate(pick[1:], start=1):
        slots.append(
            LLMSlot(
                name=f"partner_{i}",
                provider=provider,
                model=m.id,
                role="alternative perspective",
            )
        )
    panel = ReasoningPanel(slots)

    # 3. Run the pipeline with the panel wired in. Handle the
    # disambiguation pause path (auto-resume with no answers, mirroring
    # `--skip-clarify`) so the demo stays non-interactive regardless of
    # how aggressively the primary model flags weakness fields.
    primary_model = pick[0].id
    _section("running pipeline (parallel / primary-wins)")
    pending: dict = {}
    captured: dict = {}

    async def _on_event(et, **kw):
        if et == EventType.AGENT_DISAMBIGUATE:
            captured["disambig_id"] = kw.get("disambig_id")

    started = time.monotonic()
    result = await run_pipeline(
        PROMPT,
        provider=provider,
        model=primary_model,
        opts=PipelineOptions(scorer_model=primary_model),
        request_timeout=600.0,
        idle_timeout=120.0,
        reasoning_panel=panel,
        panel_mode="parallel",
        panel_aggregator="primary-wins",
        on_event=_on_event,
        pending_disambig=pending,
    )
    if (
        result.extras
        and result.extras.get("paused")
        and captured.get("disambig_id")
    ):
        print("(pipeline paused for disambiguation; auto-resuming with no answers)")
        snapshot = pending[captured["disambig_id"]]
        result = await run_pipeline(
            snapshot["prompt"],
            provider=provider,
            model=primary_model,
            opts=PipelineOptions(
                scorer_model=primary_model,
                resume_state=build_resume_state(snapshot, {}),
            ),
            request_timeout=600.0,
            idle_timeout=120.0,
            reasoning_panel=panel,
            panel_mode="parallel",
            panel_aggregator="primary-wins",
        )
    elapsed = time.monotonic() - started

    # 4. Pretty-print per-pass telemetry.
    panel_tel = (result.extras or {}).get("panel", {})
    _section("per-pass panel telemetry")
    if not panel_tel:
        print(
            "no panel telemetry recorded -- this should not happen when a "
            "panel is wired and reachable. Check pipeline error events."
        )
    for pass_name in ("pass1", "pass2", "pass3", "pass4"):
        entry = panel_tel.get(pass_name)
        if entry is None:
            print(f"\n[{pass_name}]  (not present in telemetry)")
            continue
        primary = entry.get("primary", "") or ""
        partners = entry.get("partners", []) or []
        print(f"\n[{pass_name}]")
        print(f"  primary:        {len(primary)} chars")
        for p in partners:
            err = p.get("error")
            err_str = f"  ERROR={err}" if err else ""
            print(
                f"  partner {p.get('name'):<12s} "
                f"{len(p.get('content') or ''):>6d} chars  "
                f"{p.get('ms'):>6d} ms{err_str}"
            )
        # Per-pass totals
        total_partner_chars = sum(
            len(p.get("content") or "") for p in partners
        )
        total_partner_ms = sum(p.get("ms") or 0 for p in partners)
        ok = sum(1 for p in partners if not p.get("error"))
        print(
            f"  totals:         partners ok={ok}/{len(partners)}, "
            f"sum chars={total_partner_chars}, sum ms={total_partner_ms}"
        )

    _section("aggregated result")
    print(f"technique: {result.technique}")
    print(f"task_type: {result.task_type}")
    print(f"scores:    {result.scores}")
    print(f"elapsed:   {elapsed:.2f}s")
    print(f"\nresult ({len(result.result)} chars):\n{result.result}")

    # 5. Archive a JSON trace under tools/reviews/.
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = ARCHIVE_DIR / f"panel-smoke-{stamp}.json"
    payload = {
        "prompt": PROMPT,
        "primary_model": primary_model,
        "partners": [
            {"name": s.name, "model": s.model, "role": s.role}
            for s in panel.slots[1:]
        ],
        "elapsed_s": round(elapsed, 3),
        "technique": result.technique,
        "task_type": result.task_type,
        "scores": result.scores,
        "scores_fallback": result.scores_fallback,
        "pass3_partial": result.pass3_partial,
        "pass_times_ms": result.pass_times_ms,
        "panel": panel_tel,
        "result": result.result,
    }
    archive.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\ntrace archived: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
