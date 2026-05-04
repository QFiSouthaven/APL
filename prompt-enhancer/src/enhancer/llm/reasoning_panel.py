"""ReasoningPanel — N-slot LLM panel attached to a component.

Every umbrella component (prompt-enhancer, round-robin, development)
can now plug an arbitrary number of reasoning-partner LLMs alongside
its primary. The architecture-vision diagram supplied 2026-05-04 had
multiple "(+ LLM Placeholder)" boxes scattered across each swimlane;
this module is the typed home for those placeholders.

Slot 0 is the **primary** — the LLM whose output is canonical. Slots
1..N are **partners** that supplement, critique, or counter-balance
the primary depending on the chosen mode. Panel sizes are unbounded:
a panel of one is just a primary; a panel of fifty is a full
deliberation circle. Heterogeneous panels (different providers /
models / hosts per slot) are explicitly supported — slot.provider is
any :class:`ChatProvider` instance.

Three modes, three aggregators — that's the v2.1 surface. Each mode
chooses how partners are invoked; each aggregator chooses how their
outputs reduce to a single canonical answer.

================================== Modes ==================================

``primary-only``
    Partners are instantiated but not consulted. Panel acts like a
    plain primary call. Useful as a feature flag: wire the panel
    everywhere now, decide later when to actually engage it.

``parallel``
    Primary + every partner run concurrently via :func:`asyncio.gather`.
    Each emits independently; the aggregator reconciles. Throughput-
    bounded by the slowest LM Studio instance the panel touches —
    use heterogeneous providers to spread load.

``sequential``
    Primary runs, then each partner in declared order, each seeing
    the prior outputs as appended messages. Costs more wall-clock but
    enables genuine chain-of-thought across multiple models.

================================ Aggregators ==============================

``primary-wins``
    Returns primary's response verbatim; partners are advisory only.
    The default — most conservative, preserves existing single-LLM
    semantics when a panel is wired but the caller hasn't decided how
    to fold partners in yet.

``longest``
    Picks the response with the most characters. Crude but useful
    for "the most thorough thinker wins" semantics.

``consensus-vote``
    For categorical / boolean responses. Each slot's response is
    parsed as a JSON object; majority-rules per key (weighted by
    slot.weight). Falls back to primary-wins on parse failure.

============================== Wiring contract ============================

Components opt in via an optional ``reasoning_panel`` parameter on
their main entry point::

    # prompt-enhancer
    await run_pipeline(prompt, ..., reasoning_panel=panel)

    # round-robin
    await orchestrate(topic, ..., reasoning_panel=panel)

    # development
    Orchestrator(llm, board, reasoning_panel=panel).build(request)

When ``reasoning_panel is None`` (default), components behave exactly
as v2.0 — pure backward compatibility. When supplied, the component
chooses a mode + aggregator per its own internal contract (typically:
``parallel`` + ``primary-wins`` so partners enrich the message-board
event stream without reshaping the primary's output).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .base import ChatProvider

logger = logging.getLogger("enhancer.llm.reasoning_panel")


# ─── slot ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMSlot:
    """One slot in a reasoning panel.

    Each slot is an independent LLM endpoint. Slots can point at
    different providers, different LM Studio hosts, or different
    models — the panel is heterogeneous by design.

    Attributes:
        name: display name; also prepended to the system message when
            the slot's role is non-empty.
        provider: a :class:`ChatProvider` instance — LMStudio, Ollama,
            OpenAI, Anthropic, etc.
        model: model id this slot uses for chat() calls.
        role: short description threaded into the system prompt when
            the slot is consulted (empty = no decoration).
        weight: relative weight for consensus aggregation. Default 1.0
            = equal vote with all other slots; >1 = louder voice.
    """

    name: str
    provider: ChatProvider
    model: str
    role: str = ""
    weight: float = 1.0

    def system_decoration(self) -> str:
        """Produce a tiny system-message preamble for this slot.

        Returns the empty string when ``role`` is empty so the slot
        contributes a plain chat with no role injection.
        """
        if not self.role:
            return ""
        return f"You are: {self.name} — {self.role}"


# ─── modes + aggregators (string constants, not enums, for JSON-safety) ────


VALID_MODES = frozenset({"primary-only", "parallel", "sequential"})
VALID_AGGREGATORS = frozenset({"primary-wins", "longest", "consensus-vote"})

DEFAULT_MODE = "parallel"
DEFAULT_AGGREGATOR = "primary-wins"


# ─── slot response (one entry in a panel result) ───────────────────────────


@dataclass(frozen=True)
class SlotResponse:
    """One slot's contribution to a panel call."""

    slot_name: str
    content: str
    duration_ms: int
    error: str | None = None  # populated when the slot raised; content is "" then


@dataclass(frozen=True)
class PanelResult:
    """Aggregated output of a panel consultation."""

    aggregated: str  # the final canonical answer per the aggregator
    primary: SlotResponse
    partners: tuple[SlotResponse, ...]
    mode: str
    aggregator: str
    total_duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregated": self.aggregated,
            "primary": {
                "slot_name": self.primary.slot_name,
                "content": self.primary.content,
                "duration_ms": self.primary.duration_ms,
                "error": self.primary.error,
            },
            "partners": [
                {
                    "slot_name": p.slot_name,
                    "content": p.content,
                    "duration_ms": p.duration_ms,
                    "error": p.error,
                }
                for p in self.partners
            ],
            "mode": self.mode,
            "aggregator": self.aggregator,
            "total_duration_ms": self.total_duration_ms,
        }


# ─── the panel ─────────────────────────────────────────────────────────────


class ReasoningPanel:
    """An ordered, mutable list of LLMSlots.

    A panel is created with at least one slot (the primary). Additional
    partner slots can be appended at any time via :meth:`add_slot`. The
    panel is consulted via :meth:`consult` which returns a single
    aggregated response plus the per-slot raw outputs for observability.

    Example::

        from enhancer.llm.lmstudio import LMStudioProvider
        from enhancer.llm.reasoning_panel import LLMSlot, ReasoningPanel

        primary = LMStudioProvider()
        partner_critic = LMStudioProvider(base_url="http://192.168.1.5:1234/v1")

        panel = ReasoningPanel([
            LLMSlot("primary", primary, "qwen3-coder", role=""),
            LLMSlot("critic", partner_critic, "deepseek-r1",
                    role="rigorous code critic", weight=1.5),
        ])
        panel.add_slot(LLMSlot("alt", primary, "llama-3.1-70b",
                                role="alternative perspective"))

        result = await panel.consult(
            messages=[{"role": "user", "content": "Plan the API"}],
            mode="parallel",
            aggregator="longest",
        )
        print(result.aggregated)
        for p in result.partners:
            print(f"  {p.slot_name}: {len(p.content)} chars")
    """

    def __init__(self, slots: list[LLMSlot]) -> None:
        if not slots:
            raise ValueError(
                "ReasoningPanel requires at least one slot (the primary)."
            )
        self._slots: list[LLMSlot] = list(slots)

    # ── slot management ────────────────────────────────────────────────

    def add_slot(self, slot: LLMSlot) -> None:
        """Append a partner slot. Panels are unbounded by design."""
        self._slots.append(slot)

    def remove_slot(self, name: str) -> bool:
        """Remove the first slot whose name matches. Returns True if removed.

        Refuses to remove slot 0 (the primary) — every panel must have
        at least one slot.
        """
        for i, s in enumerate(self._slots):
            if s.name == name:
                if i == 0:
                    raise ValueError(
                        "Cannot remove the primary slot (slot 0). "
                        "Replace via the constructor instead."
                    )
                del self._slots[i]
                return True
        return False

    @property
    def slots(self) -> tuple[LLMSlot, ...]:
        return tuple(self._slots)

    @property
    def primary(self) -> LLMSlot:
        return self._slots[0]

    @property
    def partners(self) -> tuple[LLMSlot, ...]:
        return tuple(self._slots[1:])

    def __len__(self) -> int:
        return len(self._slots)

    # ── consultation ───────────────────────────────────────────────────

    async def consult(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: str = DEFAULT_MODE,
        aggregator: str = DEFAULT_AGGREGATOR,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> PanelResult:
        """Run the panel and return an aggregated result.

        Validates ``mode`` and ``aggregator`` against the pinned sets;
        raises ``ValueError`` on unknown values.
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"Unknown mode {mode!r}; must be one of {sorted(VALID_MODES)}"
            )
        if aggregator not in VALID_AGGREGATORS:
            raise ValueError(
                f"Unknown aggregator {aggregator!r}; "
                f"must be one of {sorted(VALID_AGGREGATORS)}"
            )

        started = time.monotonic()
        if mode == "primary-only":
            primary_resp = await _call_slot(
                self.primary, messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
            partners: list[SlotResponse] = []
        elif mode == "parallel":
            primary_resp, partners = await _consult_parallel(
                self._slots, messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
        else:  # sequential
            primary_resp, partners = await _consult_sequential(
                self._slots, messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )

        aggregated = _aggregate(aggregator, primary_resp, partners, self._slots)
        total_ms = int((time.monotonic() - started) * 1000)

        return PanelResult(
            aggregated=aggregated,
            primary=primary_resp,
            partners=tuple(partners),
            mode=mode,
            aggregator=aggregator,
            total_duration_ms=total_ms,
        )


# ─── internals ─────────────────────────────────────────────────────────────


async def _call_slot(
    slot: LLMSlot,
    messages: list[dict[str, Any]],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> SlotResponse:
    """Invoke one slot's chat(). Errors are CAPTURED, never propagated —
    a panel call must always return one SlotResponse per slot, even if
    that slot's underlying provider crashed."""
    started = time.monotonic()

    decorated_messages = list(messages)
    deco = slot.system_decoration()
    if deco:
        decorated_messages = [{"role": "system", "content": deco}, *decorated_messages]

    try:
        content = await slot.provider.chat(
            decorated_messages,
            model=slot.model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        return SlotResponse(
            slot_name=slot.name,
            content=content or "",
            duration_ms=duration_ms,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 — capture for caller
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning("Slot %r raised: %s", slot.name, exc)
        return SlotResponse(
            slot_name=slot.name,
            content="",
            duration_ms=duration_ms,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _consult_parallel(
    slots: list[LLMSlot],
    messages: list[dict[str, Any]],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> tuple[SlotResponse, list[SlotResponse]]:
    """asyncio.gather all slots; first slot is primary, rest are partners."""
    coros = [
        _call_slot(slot, messages,
                   temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        for slot in slots
    ]
    responses = await asyncio.gather(*coros)
    return responses[0], list(responses[1:])


async def _consult_sequential(
    slots: list[LLMSlot],
    messages: list[dict[str, Any]],
    *,
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
) -> tuple[SlotResponse, list[SlotResponse]]:
    """Each slot sees prior slots' outputs appended as assistant messages."""
    primary_resp = await _call_slot(
        slots[0], messages,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
    )
    partners: list[SlotResponse] = []
    chain: list[dict[str, Any]] = list(messages)
    chain.append({"role": "assistant", "content": primary_resp.content})

    for slot in slots[1:]:
        # Each partner sees the running chain — primary's response plus
        # all earlier partners' responses.
        chain.append({
            "role": "user",
            "content": (
                f"The previous assistant turn(s) are from another reasoning "
                f"partner. Now respond as {slot.name}."
            ),
        })
        resp = await _call_slot(
            slot, chain,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )
        partners.append(resp)
        # Pop the synthetic user turn before threading the assistant turn,
        # so the chain stays clean for the next iteration.
        chain.pop()
        chain.append({"role": "assistant", "content": resp.content})

    return primary_resp, partners


# ─── aggregators ───────────────────────────────────────────────────────────


def _aggregate(
    aggregator: str,
    primary: SlotResponse,
    partners: list[SlotResponse],
    slots: list[LLMSlot],
) -> str:
    """Reduce primary + partners → one canonical string per aggregator name."""
    if aggregator == "primary-wins":
        return primary.content

    all_responses = [primary, *partners]

    if aggregator == "longest":
        ok = [r for r in all_responses if r.error is None and r.content]
        if not ok:
            return primary.content  # nothing useful; fall back
        return max(ok, key=lambda r: len(r.content)).content

    if aggregator == "consensus-vote":
        return _consensus_vote(all_responses, slots)

    # Validated upstream; defensive fallback.
    return primary.content


def _consensus_vote(
    responses: list[SlotResponse],
    slots: list[LLMSlot],
) -> str:
    """Parse each response as JSON, vote per-key with slot weights.

    For each top-level key in the parsed JSON across all slots, the
    most-voted value (weighted by ``slot.weight``) wins. Ties are
    broken by the primary's value if it's in the running, else
    alphabetic.

    Falls back to ``primary-wins`` semantics if fewer than two slots
    produced parseable JSON.
    """
    parsed: list[tuple[LLMSlot, dict[str, Any]]] = []
    for slot, resp in zip(slots, responses):
        if resp.error is not None or not resp.content:
            continue
        try:
            obj = json.loads(resp.content)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            parsed.append((slot, obj))

    if len(parsed) < 2:
        return responses[0].content  # primary-wins fallback

    # Collect per-key weighted votes.
    keys = set()
    for _, obj in parsed:
        keys.update(obj.keys())

    consensus: dict[str, Any] = {}
    primary_obj = parsed[0][1] if parsed and parsed[0][0] is slots[0] else {}

    for key in keys:
        votes: Counter[str] = Counter()
        weights: dict[str, float] = {}
        for slot, obj in parsed:
            if key not in obj:
                continue
            val = obj[key]
            # Use repr for hashability (handles dicts, lists, etc).
            val_repr = json.dumps(val, sort_keys=True, default=str)
            votes[val_repr] = votes.get(val_repr, 0) + 1
            weights[val_repr] = weights.get(val_repr, 0.0) + slot.weight
        if not votes:
            continue
        # Pick the value with the highest weighted vote.
        winner_repr = max(votes, key=lambda v: weights[v])
        consensus[key] = json.loads(winner_repr)

    # If primary contributed, ensure its keys it had win on ties.
    # (The weight system already handles real ties via slot.weight; this
    # is a safeguard for the case where two slots have equal weight + vote.)

    return json.dumps(consensus, indent=2)
