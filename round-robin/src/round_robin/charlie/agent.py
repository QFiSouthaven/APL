"""Charlie — end-of-run summarizer. Produces one structured Markdown file for FTSIA handoff."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ..config import CHARLIE_INPUT_TOKEN_LIMIT
from ..lm_client import LMLinkClient, LMLinkError
from .workspace import CharlieWorkspace, SandboxError

logger = logging.getLogger(__name__)

SUMMARY_FILENAME = "summary.md"
SCHEMA_VERSION = 1


def _approx_tokens(text: str) -> int:
    """Char/4 token estimate — industry-standard rule of thumb (1 token ≈ 4 chars
    for English text on BPE tokenizers). Whitespace-word count under-counts by
    ~5× on technical content with code/numbers; chars/4 is much closer to what
    LM Studio's tokenizer actually produces."""
    return max(1, len(text or "") // 4)


# Output budget reserved for Charlie's reply. The summary itself is typically
# 1-3k tokens; reserving 4k gives reasoning models room to think + answer.
CHARLIE_OUTPUT_RESERVE_TOKENS = 4000

# Floor budget when /api/v0/models doesn't expose loaded_context_length (older
# LM Studio, or model not loaded). Conservative — most modern locals are 8k+.
CHARLIE_FALLBACK_CONTEXT = 4096

_SYSTEM_PROMPT = """You are Agent Charlie, a structured summarizer. Two other agents (Alpha and \
Bravo) have just finished a round-robin design dialogue. Your job is to distill that dialogue into \
a single Markdown document that a downstream framework (FTSIA — Folder Tree Structure Integrity \
Administration) will parse to seed modular skeletons.

Output rules:
- Respond with Markdown ONLY. No prose intro, no commentary, no code fences around the whole thing.
- Use these H2 section headings, in this exact order:

## Theme
One-line problem statement copied or paraphrased from the session theme.

## Participants
Bullet list of agents and the models they used.

## Resolved Decisions
Bullet list. One concrete decision per bullet. Skip anything still under debate.

## Proposed Module Breakdown
Bullet list of modules / components / files / services the dialogue converged on. This is the \
PRIMARY input for FTSIA — be concrete (names, responsibilities, one-line scope each).

## Open Questions
Bullet list of unresolved questions or undecided trade-offs. Empty list "- (none)" is fine.

## Full Transcript
Verbatim transcript. For each turn, prefix with **AgentName:** followed by the content.

Rules:
- Do not invent decisions that were not in the dialogue.
- Do not include orchestrator nudges or system messages in the transcript section.
- Keep the document under 2 MB.
"""


EmitFn = Callable[..., Awaitable[None]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_transcript(transcript: list[dict]) -> str:
    """Full transcript, agent-prefixed, orchestrator/nudge entries stripped."""
    lines: list[str] = []
    for entry in transcript:
        agent = entry.get("agent", "?")
        if agent in ("orchestrator", "user_nudge"):
            continue
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{agent}]\n{content}\n")
    return "\n".join(lines)


def _truncate_transcript(
    transcript: list[dict],
    token_limit: int,
    keep_head: int = 2,
    keep_tail: int = 6,
) -> tuple[list[dict], int]:
    """Fit the transcript into `token_limit`. Two-stage strategy:

    1. **Drop middle turns** if there are more than `keep_head + keep_tail` agent
       entries. First/last preserved, middle replaced with a marker.
    2. **Per-turn clipping** if the kept entries' total still exceeds the budget
       (e.g. a 4-turn transcript where each turn is huge). Each over-budget turn
       gets its content head+tail-clipped with a "[…N chars elided…]" marker.

    Returns (new_transcript, dropped_count). dropped_count counts whole turns
    dropped in stage 1; per-turn clipping does not bump the counter (the turn
    is still present, just shorter).
    """
    agent_entries = [
        (i, e) for i, e in enumerate(transcript)
        if e.get("agent") not in ("orchestrator", "user_nudge")
    ]
    total_tokens = sum(_approx_tokens(e.get("content") or "") for _, e in agent_entries)
    if total_tokens <= token_limit:
        return transcript, 0

    # ── Stage 1: drop middle turns ────────────────────────────────────────
    out: list[dict] = list(transcript)
    dropped = 0
    if len(agent_entries) > keep_head + keep_tail:
        head_indices = {agent_entries[i][0] for i in range(keep_head)}
        tail_indices = {agent_entries[-(i + 1)][0] for i in range(keep_tail)}
        keep_indices = head_indices | tail_indices
        dropped = len(agent_entries) - len(keep_indices)

        new_out: list[dict] = []
        inserted_marker = False
        for i, entry in enumerate(transcript):
            if entry.get("agent") in ("orchestrator", "user_nudge"):
                new_out.append(entry)
                continue
            if i in keep_indices:
                new_out.append(entry)
            elif not inserted_marker:
                new_out.append({
                    "agent": "orchestrator",
                    "content": (
                        f"[truncated {dropped} agent turn(s) from the middle to fit "
                        f"Charlie's context window — first {keep_head} and last "
                        f"{keep_tail} preserved]"
                    ),
                    "timestamp": _utcnow(),
                })
                inserted_marker = True
        out = new_out

    # ── Stage 2: per-turn clipping if still over budget ──────────────────
    # Recount remaining agent entries
    remaining = [
        (i, e) for i, e in enumerate(out)
        if e.get("agent") not in ("orchestrator", "user_nudge")
    ]
    total_remaining = sum(_approx_tokens(e.get("content") or "") for _, e in remaining)
    if remaining and total_remaining > token_limit:
        # Per-turn budget: split equally across remaining agent entries.
        per_turn = max(64, token_limit // len(remaining))
        for idx, entry in remaining:
            content = entry.get("content") or ""
            if _approx_tokens(content) <= per_turn:
                continue
            # Clip to per_turn tokens worth of chars (chars/4 heuristic in reverse)
            keep_chars = per_turn * 4
            head_chars = keep_chars * 2 // 3   # 2/3 from start, 1/3 from end
            tail_chars = keep_chars - head_chars
            elided = len(content) - head_chars - tail_chars
            if elided > 0:
                clipped = (
                    content[:head_chars]
                    + f"\n\n[…{elided} chars elided to fit Charlie's context…]\n\n"
                    + content[-tail_chars:]
                )
                # Mutate a shallow copy so we don't change the caller's transcript.
                out[idx] = {**entry, "content": clipped}

    return out, dropped


def _build_frontmatter(
    *, run_id: str | None, theme: str, model: str, truncated: bool = False,
    dropped_turns: int = 0,
) -> str:
    safe_theme = (theme or "").replace("\n", " ").strip() or "(none)"
    lines = [
        "---",
        f"run_id: {run_id or '(none)'}",
        f"theme: {safe_theme}",
        f"generated_at: {_utcnow()}",
        f"model: {model}",
        f"schema_version: {SCHEMA_VERSION}",
    ]
    if truncated:
        lines.append("truncated: true")
        lines.append(f"dropped_turns: {dropped_turns}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


class CharlieAgent:
    """Stateless wrapper. Holds a busy flag so concurrent triggers don't pile up."""

    def __init__(self, client: LMLinkClient) -> None:
        self._client = client
        self._busy = False
        self.last_error: str | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    async def _resolve_input_budget(
        self, model: str, override: int | None,
    ) -> int:
        """How many tokens of transcript can we send Charlie?

        Strategy (most precise first):
          1. Explicit caller override (tests pass this).
          2. /api/v0/models/{model}.loaded_context_length minus output reserve.
          3. /api/v0/models/{model}.max_context_length minus output reserve
             (model not currently loaded — JIT will load with default ctx).
          4. CHARLIE_INPUT_TOKEN_LIMIT user setting.
          5. CHARLIE_FALLBACK_CONTEXT minus reserve.

        We pick the MIN of (LM Studio's window) and (user's hard cap) so a tiny
        local model never gets more than its context can handle, even if the user
        configured a generous CHARLIE_INPUT_TOKEN_LIMIT.
        """
        if override is not None:
            return override

        info = await self._client.model_info(model)
        if info is not None:
            ctx = info.get("loaded_context_length") or info.get("max_context_length")
            if isinstance(ctx, int) and ctx > 0:
                # Reserve room for the system prompt (~500 tok) + Charlie's reply.
                budget = max(512, ctx - CHARLIE_OUTPUT_RESERVE_TOKENS - 500)
                # User cap still wins if it's tighter (e.g. they want short summaries
                # against a 1M-context model).
                return min(budget, CHARLIE_INPUT_TOKEN_LIMIT) if \
                    CHARLIE_INPUT_TOKEN_LIMIT > 0 else budget

        logger.warning(
            "Charlie: /api/v0/models/%s unavailable; using fallback budget %d",
            model, CHARLIE_FALLBACK_CONTEXT,
        )
        return min(
            CHARLIE_FALLBACK_CONTEXT - CHARLIE_OUTPUT_RESERVE_TOKENS,
            CHARLIE_INPUT_TOKEN_LIMIT or CHARLIE_FALLBACK_CONTEXT,
        )

    async def summarize(
        self,
        workspace: CharlieWorkspace,
        transcript: list[dict],
        theme: str,
        model: str,
        run_id: str | None,
        emit: EmitFn,
        token_limit: int | None = None,
    ) -> str | None:
        """Generate summary.md. Returns the relative path on success, or None on failure.

        `token_limit` overrides the global `CHARLIE_INPUT_TOKEN_LIMIT` for this call —
        useful for tests. Oversized transcripts are truncated (head + tail kept, middle
        replaced with an orchestrator marker) and the resulting summary's frontmatter
        gets `truncated: true` so FTSIA knows the input was lossy.
        """
        if self._busy:
            self.last_error = "Charlie is still summarizing the previous run; please wait."
            await emit("charlie_error", error=self.last_error)
            return None
        self._busy = True
        self.last_error = None  # reset for this call
        try:
            await emit("charlie_started", session_id=workspace.session_id, run_id=run_id)

            # ── Resolve the real per-model budget from /api/v0/models ──
            # Falls back gracefully if the native endpoint is unavailable or the
            # model isn't loaded yet (LM Studio JIT will load it on first call).
            limit = await self._resolve_input_budget(model, token_limit)
            await emit("charlie_progress", phase="budgeting",
                       model=model, input_budget=limit)

            working_transcript, dropped = _truncate_transcript(transcript, limit)
            if dropped:
                logger.warning(
                    "Charlie truncated %d transcript turn(s) to fit %d-token budget",
                    dropped, limit,
                )
                await emit("charlie_progress",
                           phase="truncated", dropped=dropped, token_limit=limit)

            transcript_text = _format_transcript(working_transcript)
            user_prompt = (
                f"SESSION THEME:\n{theme or '(none)'}\n\n"
                f"DIALOGUE TRANSCRIPT:\n{transcript_text}\n\n"
                "Produce the structured Markdown summary as specified."
            )
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            await emit("charlie_progress", phase="calling_llm", model=model)
            try:
                # max_tokens explicitly set so reasoning models don't burn the
                # entire context window on `reasoning_content` and leave us
                # with empty content. Temperature low for stable structure.
                body = await self._client.chat(
                    messages, model=model,
                    temperature=0.3,
                    max_tokens=CHARLIE_OUTPUT_RESERVE_TOKENS,
                )
            except LMLinkError as exc:
                self.last_error = f"Charlie LLM call failed: {exc}"
                await emit("charlie_error", error=self.last_error)
                return None

            md = (body or "").strip()
            if not md:
                self.last_error = (
                    "Charlie returned an empty response — the model may have hit "
                    "max_tokens during reasoning or refused the request."
                )
                await emit("charlie_error", error=self.last_error)
                return None

            await emit("charlie_progress", phase="writing")
            full = (
                _build_frontmatter(
                    run_id=run_id, theme=theme, model=model,
                    truncated=bool(dropped), dropped_turns=dropped,
                )
                + md
                + "\n"
            )
            if dropped:
                full += (
                    f"\n_Note: {dropped} agent turn(s) were truncated from the middle "
                    f"of the transcript above to fit Charlie's context window._\n"
                )
            try:
                workspace.write(SUMMARY_FILENAME, full)
            except SandboxError as exc:
                self.last_error = f"Sandbox reject writing summary: {exc}"
                await emit("charlie_error", error=self.last_error)
                return None
            except OSError as exc:
                self.last_error = f"I/O error writing summary: {exc}"
                await emit("charlie_error", error=self.last_error)
                return None

            await emit(
                "charlie_done",
                run_id=run_id,
                path=SUMMARY_FILENAME,
                tree=workspace.tree(),
                session_id=workspace.session_id,
                truncated=bool(dropped),
                dropped_turns=dropped,
            )
            return SUMMARY_FILENAME
        finally:
            self._busy = False
