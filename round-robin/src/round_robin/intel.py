"""Dialogue intelligence — closure / agreement / redundancy detection + nudges.

Pure functions, no I/O. Orchestrator calls `DialogueAnalyzer.maybe_nudge()`
after each turn and injects the returned nudge (if any) into the transcript
before the next agent fires.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Critical-collaborator directive prepended to every persona system prompt
# when cfg.intel_collab_directive is True.
COLLAB_DIRECTIVE = (
    "Be a critical collaborator. When you agree with the other agent, identify "
    "ONE concrete improvement, edge case, or counter-example. When you disagree, "
    "name the trade-off precisely. Avoid empty agreement, padding, summary "
    "closings, and meta-commentary about the conversation. Stay on the user's "
    "stated theme."
)

# Phrases that indicate the agent is wrapping up the conversation prematurely.
_CLOSURE_PATTERNS = [
    r"\bin summary\b",
    r"\bto summari[sz]e\b",
    r"\bin conclusion\b",
    r"\blet me know if\b",
    r"\banything else\b",
    r"\bhappy to help\b",
    r"\bany (more|further) questions\b",
    r"\bwe(?:'re|'ve| are| have)? (covered|done|good|all set|finished)\b",
    r"\bready (to|for) (confirm|implement|move on|proceed)\b",
    r"\bsounds (good|great|perfect)\b",
    r"\bthat (about )?(covers|wraps) it\b",
    r"\bhope (that|this) (helps|clarifies)\b",
    r"\bfinal (answer|thoughts|version|word)\b",
    r"\bend of (discussion|conversation)\b",
    r"\bi think we'?(re| are| ve)? (done|finished|good)\b",
]
_CLOSURE_RE = re.compile("|".join(_CLOSURE_PATTERNS), re.IGNORECASE)

# Phrases that indicate uncritical agreement — used to detect echo-chamber.
_AGREEMENT_PATTERNS = [
    r"\bi agree\b",
    r"\bagreed\b",
    r"\bexactly\b",
    r"\bgreat point\b",
    r"\bwell said\b",
    r"\bthat'?s right\b",
    r"\bgood (call|idea|point|thinking)\b",
    r"\+1\b",
    r"\blove (it|that|this|the idea)\b",
    r"\bperfect\b",
    r"\bspot on\b",
    r"\bmakes sense\b",
    r"\bsounds (good|great)\b",
    r"\bcouldn'?t agree more\b",
    r"\babsolutely\b",
    r"\byou'?re (absolutely )?right\b",
]
_AGREEMENT_RE = re.compile("|".join(_AGREEMENT_PATTERNS), re.IGNORECASE)

# Tokens we strip when computing word-overlap (stopwords + punctuation noise).
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "must", "can", "this", "that", "these", "those", "i", "you",
    "we", "they", "it", "he", "she", "as", "so", "than", "too", "very", "just",
    "not", "no", "yes",
})

_WORD_RE = re.compile(r"[a-zA-Z']+")


@dataclass(frozen=True)
class Nudge:
    reason: str           # 'closure' | 'redundant' | 'brief' | 'agreement_streak'
    content: str


@dataclass
class IntelConfig:
    """Subset of RunConfig that the analyzer cares about."""
    anti_rambling: bool = True
    anti_yes_man: bool = True
    redundancy_threshold: float = 0.7
    brief_threshold_tokens: int = 30
    agreement_threshold: int = 2


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")
            if w.lower() not in _STOPWORDS]


def closure_signals(text: str) -> list[str]:
    """Return distinct closure phrases found in `text`."""
    return list({m.group(0).lower() for m in _CLOSURE_RE.finditer(text or "")})


def agreement_signals(text: str) -> list[str]:
    """Return distinct agreement phrases found in `text`."""
    return list({m.group(0).lower() for m in _AGREEMENT_RE.finditer(text or "")})


def redundancy_score(text: str, prev_own_text: str) -> float:
    """Word-overlap ratio (Jaccard on content words). 0.0 = unique, 1.0 = identical."""
    if not text or not prev_own_text:
        return 0.0
    a = set(_tokens(text))
    b = set(_tokens(prev_own_text))
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


def token_count(text: str) -> int:
    """Cheap whitespace token estimate. Keep in sync with orchestrator helper."""
    return len((text or "").split())


class DialogueAnalyzer:
    """Stateless analyzer; cross-turn state lives in the orchestrator."""

    @staticmethod
    def previous_turn_by_agent(transcript: list[dict], agent_name: str) -> dict | None:
        """Find the most recent transcript entry authored by this agent."""
        for entry in reversed(transcript[:-1]):  # skip the just-appended turn
            if entry.get("agent") == agent_name and not entry.get("skipped"):
                return entry
        return None

    @staticmethod
    def maybe_nudge(
        transcript: list[dict],
        agent_name: str,
        turns_remaining: int,
        cfg: IntelConfig,
    ) -> Nudge | None:
        """Inspect the last turn (by agent_name). Return a Nudge or None.

        `transcript` must include the just-completed turn at the tail.
        `turns_remaining` is how many agent-turns remain after this one.
        Returns None on the last turn — nudges only fire when there's room for
        the next agent to actually respond to them.
        """
        if turns_remaining < 1:
            return None
        if not transcript:
            return None

        last = transcript[-1]
        if last.get("agent") != agent_name:
            return None
        text = last.get("content") or ""
        if not text.strip():
            return None

        if cfg.anti_rambling:
            closures = closure_signals(text)
            if closures:
                return Nudge(
                    reason="closure",
                    content=(
                        f"You're not done — there are {turns_remaining} rounds "
                        "remaining and the theme isn't fully explored. Pick the "
                        "weakest part of the current direction and propose a "
                        "concrete course-correction. Don't summarize."
                    ),
                )

            prev = DialogueAnalyzer.previous_turn_by_agent(transcript, agent_name)
            if prev:
                score = redundancy_score(text, prev.get("content") or "")
                if score >= cfg.redundancy_threshold:
                    return Nudge(
                        reason="redundant",
                        content=(
                            "That repeats your previous turn. Introduce a new "
                            "angle, constraint, or counter-example that hasn't "
                            "been examined yet."
                        ),
                    )

            tcount = token_count(text)
            # Use the position in the transcript (excluding system seed + nudges) as
            # a turn proxy; only flag brevity past the warmup.
            real_turns = sum(
                1 for e in transcript
                if e.get("agent") not in ("orchestrator", "user_nudge")
            )
            if tcount < cfg.brief_threshold_tokens and real_turns > 2:
                return Nudge(
                    reason="brief",
                    content=(
                        f"Brief responses are fine when warranted, but "
                        f"{turns_remaining} rounds remain. Either dig deeper into "
                        "a specific sub-problem or explicitly state what's "
                        "blocking further progress."
                    ),
                )
        return None

    @staticmethod
    def contrarian_nudge(streak: int) -> Nudge:
        """Build a contrarian nudge after `streak` consecutive agreements."""
        return Nudge(
            reason="agreement_streak",
            content=(
                f"You've agreed {streak} turns in a row. Take the opposite "
                "stance for one round — argue why the current direction might "
                "be wrong, what assumptions are unexamined, or what alternative "
                "is actually stronger. Be specific."
            ),
        )

    @staticmethod
    def has_agreement(text: str) -> bool:
        return bool(agreement_signals(text))


def intel_config_from_run(cfg: Any) -> IntelConfig:
    """Build an IntelConfig from a RunConfig (or compatible shape)."""
    return IntelConfig(
        anti_rambling=getattr(cfg, "intel_anti_rambling", True),
        anti_yes_man=getattr(cfg, "intel_anti_yes_man", True),
        redundancy_threshold=getattr(cfg, "intel_redundancy_threshold", 0.7),
        brief_threshold_tokens=getattr(cfg, "intel_brief_threshold_tokens", 30),
        agreement_threshold=getattr(cfg, "intel_agreement_threshold", 2),
    )
