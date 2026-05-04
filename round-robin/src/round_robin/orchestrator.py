"""Round Robin orchestrator. State machine with pause/resume/retry/skip/use-other.

Emits typed events through an injected callback (the FastAPI server hooks this
to a WebSocket broadcast). Owns no I/O of its own beyond LMLinkClient calls and
SafeStorage state writes.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .charlie import CharlieAgent, CharlieWorkspace, new_session as charlie_new_session
from .intel import COLLAB_DIRECTIVE, DialogueAnalyzer, intel_config_from_run
from .lm_client import LMLinkClient, LMLinkError
from .storage import SafeStorage
from .config import STATE_FILE
from .reasoning_panel import ReasoningPanel  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_AWAITING_USER = "awaiting_user"   # error recovery — user picks retry/skip/other
STATUS_DONE = "done"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"


@dataclass
class AgentConfig:
    name: str
    model: str
    persona: str = ""


@dataclass
class CharlieConfig:
    enabled: bool = False
    model: str = ""


@dataclass
class RunConfig:
    theme: str
    agents: list[AgentConfig]
    loop_limit: int = 3
    pause_after_each_turn: bool = False
    auto_retry: int = 0   # number of automatic retries before asking the user
    auto_retry_backoff_s: float = 2.0
    charlie: CharlieConfig = field(default_factory=CharlieConfig)
    # Dialogue intelligence
    intel_collab_directive: bool = True
    intel_anti_rambling: bool = True
    intel_anti_yes_man: bool = True
    intel_redundancy_threshold: float = 0.7
    intel_brief_threshold_tokens: int = 30
    intel_agreement_threshold: int = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _replace_agents(config: RunConfig, agents: list[AgentConfig]) -> RunConfig:
    """Return a copy of ``config`` with ``agents`` replaced.

    Used when a ReasoningPanel is supplied — the slot list defines the
    agents and we don't want to mutate the caller's RunConfig.
    """
    return RunConfig(
        theme=config.theme,
        agents=agents,
        loop_limit=config.loop_limit,
        pause_after_each_turn=config.pause_after_each_turn,
        auto_retry=config.auto_retry,
        auto_retry_backoff_s=config.auto_retry_backoff_s,
        charlie=config.charlie,
        intel_collab_directive=config.intel_collab_directive,
        intel_anti_rambling=config.intel_anti_rambling,
        intel_anti_yes_man=config.intel_anti_yes_man,
        intel_redundancy_threshold=config.intel_redundancy_threshold,
        intel_brief_threshold_tokens=config.intel_brief_threshold_tokens,
        intel_agreement_threshold=config.intel_agreement_threshold,
    )


def _build_messages(
    agent: AgentConfig,
    theme: str,
    transcript: list[dict],
    *,
    collab_directive: bool = True,
) -> list[dict]:
    system_parts = [agent.persona or f"You are {agent.name}. Keep responses concise."]
    if collab_directive:
        system_parts.append(COLLAB_DIRECTIVE)
    if theme:
        system_parts.append(f"Session Theme: {theme}")
    msgs: list[dict] = [{"role": "system", "content": "\n\n".join(system_parts)}]
    for entry in transcript:
        sender = entry.get("agent", "unknown")
        content = entry.get("content", "") or ""
        if sender == agent.name:
            msgs.append({"role": "assistant", "content": content})
        elif sender == "user_nudge":
            msgs.append({"role": "user", "content": f"[orchestrator nudge]: {content}"})
        else:
            msgs.append({"role": "user", "content": f"[{sender}]: {content}"})
    return msgs


class Orchestrator:
    def __init__(self, client: LMLinkClient, emit: EmitFn) -> None:
        self._client = client
        self._emit = emit
        self._task: asyncio.Task | None = None
        self._stop = False
        self._pause_requested = False
        self._resume_event = asyncio.Event()
        self._user_choice: dict[str, Any] | None = None
        self._user_choice_event = asyncio.Event()
        self._charlie_agent = CharlieAgent(client)
        self._charlie_workspace: CharlieWorkspace | None = None
        self._agreement_streak: int = 0

        # Optional ReasoningPanel — when set, slot.provider.chat() drives each
        # turn instead of self._client.chat_stream(). slot 0 = Agent A, slot 1
        # = Agent B, slots 2..N append additional rounds. None preserves the
        # existing 2-LLM dialogue byte-for-byte.
        self._reasoning_panel: ReasoningPanel | None = None  # type: ignore[valid-type]
        self._slot_by_name: dict[str, Any] = {}

        self.run_id: str | None = None
        self.config: RunConfig | None = None
        self.status: str = STATUS_IDLE
        self.transcript: list[dict] = []
        self.current_turn: int = 0
        self.current_agent_idx: int = 0
        self.summary_path: str | None = None

    # ── External controls ─────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        config: RunConfig,
        *,
        reasoning_panel: ReasoningPanel | None = None,  # type: ignore[valid-type]
    ) -> str:
        if self.is_running():
            await self.stop()

        # When a ReasoningPanel is supplied, its slots define the agents
        # (slot 0 = Agent A, slot 1 = Agent B, slots 2..N = additional
        # rounds). The panel must contain >= 2 slots so the existing
        # 2-LLM minimum still holds. config.agents is replaced with the
        # slot-derived list; everything else (loop_limit, intel toggles,
        # charlie) flows through unchanged.
        self._reasoning_panel = reasoning_panel
        self._slot_by_name = {}
        if reasoning_panel is not None:
            slots = list(reasoning_panel.slots)
            if len(slots) < 2:
                raise ValueError(
                    "ReasoningPanel must contain at least 2 slots "
                    "(round-robin's 2-LLM dialogue minimum)."
                )
            derived_agents = [
                AgentConfig(name=s.name, model=s.model, persona=s.role)
                for s in slots
            ]
            config = _replace_agents(config, derived_agents)
            self._slot_by_name = {s.name: s for s in slots}

        if len(config.agents) < 2:
            raise ValueError("At least 2 agents are required.")

        self.run_id = uuid.uuid4().hex[:12]
        self.config = config
        self.status = STATUS_RUNNING
        self._stop = False
        self._pause_requested = False
        self._resume_event.set()
        self._user_choice = None
        self._user_choice_event.clear()
        self.transcript = [{
            "agent": "orchestrator",
            "content": f"Theme: {config.theme}",
            "timestamp": _utcnow(),
        }]
        self.current_turn = 0
        self.current_agent_idx = 0
        self.summary_path = None
        self._agreement_streak = 0
        # Workspace is lazy-created at end-of-run when we actually summarize.
        self._charlie_workspace = None

        self._save_state()
        await self._emit("run_started", run_id=self.run_id, config=self._config_dict())
        self._task = asyncio.create_task(self._run_loop())
        return self.run_id

    async def stop(self) -> None:
        self._stop = True
        # Unblock anything waiting
        self._resume_event.set()
        self._user_choice = {"action": "stop"}
        self._user_choice_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.status = STATUS_STOPPED
        self._save_state()
        await self._run_charlie_summary()
        await self._emit("run_done", status=self.status, turns_completed=self.current_turn,
                         run_id=self.run_id)

    async def pause(self) -> None:
        if not self.is_running():
            return
        self._pause_requested = True

    async def resume(self, injection: str | None = None) -> None:
        if injection:
            self.transcript.append({
                "agent": "user_nudge",
                "content": injection.strip(),
                "timestamp": _utcnow(),
            })
        self.status = STATUS_RUNNING
        self._save_state()
        await self._emit("run_resumed", injection=injection)
        self._resume_event.set()

    async def submit_choice(self, action: str, **kwargs: Any) -> None:
        """User responds to an agent error. action ∈ {retry, skip, use_other, stop}."""
        self._user_choice = {"action": action, **kwargs}
        self._user_choice_event.set()

    # ── Internal main loop ────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = self.config
        assert cfg is not None
        try:
            while self.current_turn < cfg.loop_limit and not self._stop:
                while self.current_agent_idx < len(cfg.agents):
                    if self._stop:
                        break
                    agent = cfg.agents[self.current_agent_idx]
                    await self._run_one_turn(agent)
                    if self._stop:
                        break
                    self.current_agent_idx += 1

                    if cfg.pause_after_each_turn or self._pause_requested:
                        await self._enter_pause()
                        if self._stop:
                            break

                if self._stop:
                    break
                self.current_agent_idx = 0
                self.current_turn += 1

            if not self._stop:
                self.status = STATUS_DONE
                self._save_state()
                await self._run_charlie_summary()
                await self._emit("run_done", status=self.status,
                                 turns_completed=self.current_turn, run_id=self.run_id)
        except asyncio.CancelledError:
            self.status = STATUS_STOPPED
            self._save_state()
            raise
        except Exception as exc:
            logger.exception("Orchestrator loop crashed")
            self.status = STATUS_ERROR
            self._save_state()
            await self._run_charlie_summary()
            await self._emit("run_done", status=self.status,
                             turns_completed=self.current_turn, run_id=self.run_id,
                             error=str(exc))

    async def _enter_pause(self) -> None:
        self.status = STATUS_PAUSED
        self._pause_requested = False
        self._resume_event.clear()
        self._save_state()
        await self._emit("run_paused", reason="manual_or_per_turn_toggle")
        await self._resume_event.wait()

    async def _run_one_turn(self, agent: AgentConfig) -> None:
        cfg = self.config
        assert cfg is not None
        attempt = 0
        while True:
            await self._emit("turn_started", turn=self.current_turn,
                             agent_name=agent.name, model=agent.model,
                             total_turns=cfg.loop_limit)
            messages = _build_messages(
                agent, cfg.theme, self.transcript,
                collab_directive=cfg.intel_collab_directive,
            )
            full_text = ""
            t0 = time.monotonic()
            try:
                slot = self._slot_by_name.get(agent.name) if self._reasoning_panel else None
                if slot is not None:
                    # Panel path: call the slot's provider via chat() and
                    # emit the result as a single synthetic chunk so the
                    # event stream shape is preserved for downstream UI.
                    full_text = await slot.provider.chat(
                        messages, model=slot.model,
                    )
                    if full_text:
                        await self._emit(
                            "turn_chunk", turn=self.current_turn,
                            agent_name=agent.name, token=full_text,
                        )
                    if self._stop:
                        return
                else:
                    async for token in self._client.chat_stream(messages, model=agent.model):
                        if self._stop:
                            return
                        full_text += token
                        await self._emit("turn_chunk", turn=self.current_turn,
                                         agent_name=agent.name, token=token)
                latency_ms = int((time.monotonic() - t0) * 1000)
                self.transcript.append({
                    "agent": agent.name,
                    "model": agent.model,
                    "content": full_text,
                    "latency_ms": latency_ms,
                    "timestamp": _utcnow(),
                })
                self._save_state()
                await self._emit("turn_done", turn=self.current_turn,
                                 agent_name=agent.name, content=full_text,
                                 latency_ms=latency_ms,
                                 token_count=_approx_token_count(full_text))
                await self._maybe_inject_nudge(agent, full_text)
                return
            except (LMLinkError, asyncio.TimeoutError) as exc:
                if attempt < cfg.auto_retry:
                    attempt += 1
                    await self._emit("agent_error", turn=self.current_turn,
                                     agent_name=agent.name, error_class=type(exc).__name__,
                                     message=f"{exc} — auto-retrying ({attempt}/{cfg.auto_retry})",
                                     auto_retry=True)
                    await asyncio.sleep(cfg.auto_retry_backoff_s * attempt)
                    continue
                action = await self._await_user_recovery(agent, exc)
                if action == "retry":
                    attempt = 0
                    continue
                if action == "skip":
                    self.transcript.append({
                        "agent": agent.name,
                        "content": "(skipped after error)",
                        "skipped": True,
                        "timestamp": _utcnow(),
                    })
                    self._save_state()
                    return
                if action == "use_other":
                    other = self._other_agent(agent)
                    if other is None:
                        await self._emit("agent_error", turn=self.current_turn,
                                         agent_name=agent.name, error_class="config",
                                         message="No other agent available for fallback.")
                        return
                    agent = other
                    attempt = 0
                    continue
                # stop
                self._stop = True
                return

    async def _await_user_recovery(self, agent: AgentConfig, exc: Exception) -> str:
        self.status = STATUS_AWAITING_USER
        self._save_state()
        self._user_choice = None
        self._user_choice_event.clear()
        await self._emit("agent_error", turn=self.current_turn, agent_name=agent.name,
                         error_class=type(exc).__name__, message=str(exc))
        await self._user_choice_event.wait()
        choice = self._user_choice or {"action": "stop"}
        self.status = STATUS_RUNNING
        self._save_state()
        return str(choice.get("action") or "stop")

    def _other_agent(self, current: AgentConfig) -> AgentConfig | None:
        if not self.config:
            return None
        for a in self.config.agents:
            if a.name != current.name:
                return a
        return None

    def _turns_remaining(self) -> int:
        """How many agent-turns will fire AFTER the just-completed one."""
        cfg = self.config
        if not cfg:
            return 0
        n_agents = len(cfg.agents)
        completed_in_round = self.current_agent_idx + 1   # this turn just completed
        rounds_left_after_this = cfg.loop_limit - self.current_turn - 1
        return max(0, (n_agents - completed_in_round) + rounds_left_after_this * n_agents)

    async def _maybe_inject_nudge(self, agent: AgentConfig, full_text: str) -> None:
        cfg = self.config
        if not cfg:
            return
        intel_cfg = intel_config_from_run(cfg)
        turns_remaining = self._turns_remaining()

        # Anti-yes-man: track agreement streak across turns.
        if cfg.intel_anti_yes_man and DialogueAnalyzer.has_agreement(full_text):
            self._agreement_streak += 1
        else:
            self._agreement_streak = 0

        nudge = None
        if (cfg.intel_anti_yes_man
                and self._agreement_streak >= intel_cfg.agreement_threshold
                and turns_remaining >= 1):
            nudge = DialogueAnalyzer.contrarian_nudge(self._agreement_streak)
            self._agreement_streak = 0   # don't loop on the same streak
        else:
            nudge = DialogueAnalyzer.maybe_nudge(
                self.transcript, agent.name, turns_remaining, intel_cfg
            )

        if nudge is None:
            return

        self.transcript.append({
            "agent": "user_nudge",
            "content": nudge.content,
            "intel_reason": nudge.reason,
            "timestamp": _utcnow(),
        })
        self._save_state()
        await self._emit(
            "dialogue_nudge",
            reason=nudge.reason,
            content=nudge.content,
            turn=self.current_turn,
            after_agent=agent.name,
        )

    async def regenerate_summary(self, *, model: str | None = None,
                                  transcript: list[dict] | None = None,
                                  theme: str | None = None,
                                  run_id: str | None = None) -> str | None:
        """Manual re-run of Charlie. Refuses while a run is active.

        Defaults to the orchestrator's last run (transcript, theme, model). Caller
        may override any field — e.g. to summarize a historical session.
        """
        if self.is_running() or self.status in (STATUS_PAUSED, STATUS_AWAITING_USER):
            raise RuntimeError("Cannot regenerate summary while a run is active.")

        cfg = self.config
        use_transcript = transcript if transcript is not None else list(self.transcript)
        use_theme = theme if theme is not None else (cfg.theme if cfg else "")
        use_model = model or (cfg.charlie.model if cfg else "")
        use_run_id = run_id or self.run_id

        if not use_model:
            raise ValueError("No Charlie model specified (and no run config to fall back on).")
        agent_turns = [
            e for e in use_transcript
            if e.get("agent") not in ("orchestrator", "user_nudge")
        ]
        if not agent_turns:
            raise ValueError("Transcript is empty — nothing to summarize.")

        # Reuse the existing workspace if present so the file overwrites in place;
        # otherwise spin up a fresh session.
        if self._charlie_workspace is None:
            self._charlie_workspace = charlie_new_session()

        path = await self._charlie_agent.summarize(
            workspace=self._charlie_workspace,
            transcript=use_transcript,
            theme=use_theme,
            model=use_model,
            run_id=use_run_id,
            emit=self._emit,
        )
        if path:
            self.summary_path = path
        return path

    async def _run_charlie_summary(self) -> None:
        """End-of-run: feed the full transcript to Charlie, write summary.md.

        Gated on charlie.enabled + charlie.model + a non-empty transcript (i.e. at
        least one real agent turn beyond the seed orchestrator entry).
        """
        cfg = self.config
        if not cfg or not cfg.charlie.enabled or not cfg.charlie.model:
            return
        agent_turns = [
            e for e in self.transcript
            if e.get("agent") not in ("orchestrator", "user_nudge")
        ]
        if not agent_turns:
            return
        if self._charlie_workspace is None:
            # Use new_session() so the module-level "current" workspace is set —
            # the /api/charlie/file route reads from that.
            self._charlie_workspace = charlie_new_session()
        try:
            path = await self._charlie_agent.summarize(
                workspace=self._charlie_workspace,
                transcript=list(self.transcript),
                theme=cfg.theme,
                model=cfg.charlie.model,
                run_id=self.run_id,
                emit=self._emit,
            )
            if path:
                self.summary_path = path
        except Exception as exc:
            logger.exception("Charlie summary failed")
            await self._emit("charlie_error", error=str(exc))

    # ── State persistence ─────────────────────────────────────────────────

    def _config_dict(self) -> dict:
        cfg = self.config
        if cfg is None:
            return {}
        return {
            "theme": cfg.theme,
            "agents": [{"name": a.name, "model": a.model, "persona": a.persona} for a in cfg.agents],
            "loop_limit": cfg.loop_limit,
            "pause_after_each_turn": cfg.pause_after_each_turn,
            "auto_retry": cfg.auto_retry,
            "auto_retry_backoff_s": cfg.auto_retry_backoff_s,
            "charlie": {"enabled": cfg.charlie.enabled, "model": cfg.charlie.model},
            "intel_collab_directive": cfg.intel_collab_directive,
            "intel_anti_rambling": cfg.intel_anti_rambling,
            "intel_anti_yes_man": cfg.intel_anti_yes_man,
            "intel_redundancy_threshold": cfg.intel_redundancy_threshold,
            "intel_brief_threshold_tokens": cfg.intel_brief_threshold_tokens,
            "intel_agreement_threshold": cfg.intel_agreement_threshold,
        }

    def _save_state(self) -> None:
        SafeStorage.save_json(STATE_FILE, {
            "run_id": self.run_id,
            "status": self.status,
            "current_turn": self.current_turn,
            "current_agent_idx": self.current_agent_idx,
            "transcript": self.transcript,
            "config": self._config_dict(),
            "charlie_session": self._charlie_workspace.session_id if self._charlie_workspace else None,
            "updated_at": _utcnow(),
        })

    def public_state(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "current_turn": self.current_turn,
            "current_agent_idx": self.current_agent_idx,
            "transcript": self.transcript,
            "config": self._config_dict(),
            "charlie_tree": self._charlie_workspace.tree() if self._charlie_workspace else None,
            "charlie_session_id": self._charlie_workspace.session_id if self._charlie_workspace else None,
            "summary_path": self.summary_path,
        }


def _approx_token_count(text: str) -> int:
    """Cheap whitespace-based token estimate. Good enough for UI."""
    return len(text.split())
