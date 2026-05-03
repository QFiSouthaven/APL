#!/usr/bin/env python3
"""Methodology Enhancement Agent — passive quality lift.

Invoked from a Claude Code ``Stop`` hook. Reads the latest staged diff
(or last commit) and asks the local LM Studio one structured prompt:

  1. One step ahead — what the user is likely to ask next given this diff.
  2. One step behind — silent failure modes / contract drift / concurrency
     risk introduced by this diff.
  3. A 3-line patch suggestion if the issue is mechanical (else "n/a").

Output dropped into ``tools/reviews/method-YYYYMMDD-HHMMSS.md``. Never
raises; failures degrade silently. Disable with
``ENHANCER_METHODOLOGY_AGENT_ENABLED=0``.

Designed to enforce the architectural directives in the build plan:
serial Pass 1/2, Pass 4 awaited before Magnitude/SoT, idle_timeout=120
on every stream, OutputContract version freezes, SQLite vs JSONL drift,
ChatProvider abstraction integrity.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEWS_DIR = REPO_ROOT / "tools" / "reviews"
REVIEWS_DIR.mkdir(parents=True, exist_ok=True)

LMS_BASE_URL = os.environ.get("ENHANCER_LMS_BASE_URL", "http://127.0.0.1:1234/v1")
TIMEOUT = float(os.environ.get("ENHANCER_METHODOLOGY_TIMEOUT", "60"))
ENABLED = os.environ.get("ENHANCER_METHODOLOGY_AGENT_ENABLED", "1") not in {"0", "false", "no"}

SYSTEM_PROMPT = """You are the Methodology Enhancement Agent for the prompt-enhancer project.
You read code diffs and surface what an experienced senior dev would.

Architectural directives you must enforce:
1. Pass 1 → Pass 2 are STRICTLY SERIAL — never asyncio.gather them.
2. Pass 4 must be AWAITED BEFORE any Magnitude or SoT stream begins.
3. Every chat_stream call must keep idle_timeout=120 (LM Link stalls silently).
4. EventType enum + payload schema in core/events.py is FROZEN — bump v2 on change.
5. ChatProvider ABC must not leak transport details; every backend implements the same 3 methods.
6. JSONL log format byte-for-byte matches the source monolith for one release (devflow.py compat).
7. scores_fallback and pass3_partial flags are part of the public contract.

Output exactly three short sections in markdown:
## One step ahead
## One step behind
## Patch suggestion
"""

USER_TEMPLATE = """Review this diff for the prompt-enhancer extraction work.

```
{diff}
```

Be terse. Cite file:line where possible. If nothing meaningful to flag, say so under each section.
"""


def _get_diff() -> str:
    """Prefer staged diff; fall back to last commit."""
    try:
        staged = subprocess.run(
            ["git", "diff", "--staged"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if staged.returncode == 0 and staged.stdout.strip():
            return staged.stdout
        last = subprocess.run(
            ["git", "show", "--stat", "-p", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        return last.stdout if last.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _truncate(text: str, max_chars: int = 12000) -> str:
    """First 20% + last 80% — same rule the pipeline uses."""
    if len(text) <= max_chars:
        return text
    head = max_chars // 5
    tail = max_chars - head - 64
    return f"{text[:head]}\n[...{len(text) - head - tail} chars truncated...]\n{text[-tail:]}"


_EMPTY_CONTENT_NOTICE = (
    "_methodology agent returned empty content. Likely a reasoning-token "
    "model whose thinking is filtered out of the streamed deltas. Set "
    "`ENHANCER_METHODOLOGY_MODEL` to a non-reasoning model, or confirm "
    "streaming SSE is reaching this script._"
)


def _ask_lms(diff: str) -> str:
    """Streaming call to LM Studio.

    Streaming mirrors the Pass 4 fix: non-streaming chat returns empty
    `content` against reasoning-token models (gpt-oss-120b) because the
    reasoning channel is filtered before `message.content` is populated.
    Streaming concatenates `delta.content` and bypasses that filter.

    Failures degrade to a visible diagnostic string — never silent empty.
    """
    body = {
        "model": os.environ.get("ENHANCER_METHODOLOGY_MODEL", ""),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(diff=_truncate(diff))},
        ],
        "stream": True,
        "temperature": 0.4,
        "max_tokens": 600,
    }
    if not body["model"]:
        body.pop("model")
    try:
        chunks: list[str] = []
        with httpx.Client(timeout=TIMEOUT) as client:
            with client.stream(
                "POST", f"{LMS_BASE_URL}/chat/completions", json=body
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                        delta = (
                            obj.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content")
                        )
                        if delta:
                            chunks.append(delta)
                    except (KeyError, IndexError, ValueError):
                        continue
        content = "".join(chunks).strip()
        return content if content else _EMPTY_CONTENT_NOTICE
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return f"_methodology agent unavailable: {exc!s}_"


def main() -> int:
    if not ENABLED:
        return 0
    diff = _get_diff()
    if not diff.strip():
        return 0
    review = _ask_lms(diff)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = REVIEWS_DIR / f"method-{ts}.md"
    out.write_text(
        f"# Methodology review — {ts}\n\n"
        f"_passive agent; senior-dev one-step-ahead/behind on the latest diff_\n\n"
        f"{review}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # never raise from a Stop hook
        sys.exit(0)
