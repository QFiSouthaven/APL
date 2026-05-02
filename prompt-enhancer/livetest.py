"""Direct pipeline live test — bypasses typer/rich so output is plain stdout."""

from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "src")

from enhancer.config import db_path, jsonl_log_path, load
from enhancer.core.events import EventType
from enhancer.core.pipeline import PipelineOptions, run_pipeline
from enhancer.llm.lmstudio import LMStudioProvider
from enhancer.persistence import runs as runs_module


MODEL = "gptoss-120b-uncensored-hauhaucs-aggressive"
PROMPT = "Make me a customer-support chatbot for a small SaaS startup"


async def main() -> int:
    provider = LMStudioProvider()
    settings = load()
    print(f"DB path: {db_path()}", flush=True)
    print(f"Provider: lmstudio @ {provider.base_url}", flush=True)
    print(f"Model: {MODEL}", flush=True)
    print(f"Prompt: {PROMPT!r}", flush=True)
    print("---- starting pipeline ----", flush=True)

    events_seen = []
    t0 = time.monotonic()

    async def on_event(event_type, **payload):
        name = event_type.value if hasattr(event_type, "value") else str(event_type)
        events_seen.append(name)
        elapsed = time.monotonic() - t0
        # Compact log lines so we can see exactly what fires
        if name == EventType.AGENT_PASS_START.value:
            print(f"[+{elapsed:5.1f}s] >>> START Pass {payload.get('pass_number')}: "
                  f"{payload.get('pass_name')}", flush=True)
        elif name == EventType.AGENT_PASS_RESULT.value:
            content = payload.get("content", "")
            print(f"[+{elapsed:5.1f}s] <<< DONE  Pass {payload.get('pass_number')}: "
                  f"{len(content)}ch in {payload.get('duration_ms')}ms", flush=True)
        elif name == EventType.AGENT_PASS_CHUNK.value:
            # Flush a tiny indicator per chunk so we see progress
            sys.stdout.write("."); sys.stdout.flush()
        elif name == EventType.AGENT_ERROR.value:
            print(f"\n[+{elapsed:5.1f}s] !!! ERROR step={payload.get('step')}: "
                  f"{payload.get('error')}", flush=True)
        elif name == EventType.ENHANCEMENT_SCORE.value:
            print(f"\n[+{elapsed:5.1f}s] === SCORES: {payload.get('scores')} "
                  f"fallback={payload.get('scores_fallback')}", flush=True)
        elif name == EventType.AGENT_DONE.value:
            print(f"\n[+{elapsed:5.1f}s] === AGENT_DONE", flush=True)
        else:
            print(f"\n[+{elapsed:5.1f}s] {name}", flush=True)

    try:
        result = await run_pipeline(
            PROMPT,
            provider=provider, model=MODEL,
            opts=PipelineOptions(
                temperature=0.7, max_tokens_scale=1.5,
            ),
            on_event=on_event,
            request_timeout=600.0, idle_timeout=120.0,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"\n[+{elapsed:5.1f}s] PIPELINE EXCEPTION: {type(exc).__name__}: {exc}",
              flush=True)
        import traceback
        traceback.print_exc()
        return 1

    elapsed = time.monotonic() - t0
    print(f"\n---- pipeline finished in {elapsed:.1f}s ----", flush=True)
    print(f"Result length: {len(result.result)} chars", flush=True)
    print(f"Task type: {result.task_type}", flush=True)
    print(f"Technique: {result.technique}", flush=True)
    print(f"Scores: {result.scores}", flush=True)
    print(f"scores_fallback: {result.scores_fallback}", flush=True)
    print(f"pass3_partial: {result.pass3_partial}", flush=True)
    print(f"Pass times: {result.pass_times_ms}", flush=True)
    print(f"Events seen ({len(events_seen)}): "
          f"{json.dumps([e for e in events_seen if e != EventType.AGENT_PASS_CHUNK.value])}",
          flush=True)
    print("\n---- ENHANCED PROMPT ----", flush=True)
    print(result.result, flush=True)

    # Persist
    record = result.extras.get("_record") if result.extras else None
    if record:
        runs_module.save(record, db_path(), jsonl_log_path())
        print(f"\nSaved run id: {record.id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
