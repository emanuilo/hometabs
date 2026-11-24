"""Each chat's agent, its lock and its saved Claude session."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace
from pathlib import Path

from loguru import logger

from ..agent import BrowsingAgent, instructions_fingerprint
from ..config import AgentConfig
from ..skills import build_plugin
from .model_menu import ModelMenu
from .stores import SessionStore


@dataclass
class ChatState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent: BrowsingAgent | None = None
    # Bumped by /stop and /new so messages queued before them are dropped.
    epoch: int = 0


class Chats:
    """The agents of all chats, created with the current model and effort."""

    def __init__(
        self, base: AgentConfig, state_dir: Path, sessions: SessionStore, menu: ModelMenu
    ) -> None:
        self.base = base
        self.state_dir = state_dir
        self.sessions = sessions
        self.menu = menu
        self._chats: dict[int, ChatState] = {}
        # Our skills + Aside's builtin ones from the local install, rebuilt
        # on every start so an Aside update is picked up.
        self.plugin = build_plugin(state_dir / "plugin", base.aside_skills_dir)
        logger.info("Skills: {} (plugin {})", ", ".join(self.plugin.skills), self.plugin.path)
        # Model/effort are deliberately not part of the fingerprint: a session
        # resumes fine under a different --model/--effort.
        self.fingerprint = instructions_fingerprint(self.plugin)

    def chat(self, chat_id: int) -> ChatState:
        return self._chats.setdefault(chat_id, ChatState())

    def is_busy(self, chat_id: int | None) -> bool:
        """Whether *chat_id* is mid-turn. Agents with outdated options are not
        closed here: :meth:`ready_agent` reconnects them (resuming the session)
        before the next turn, which never waits on a running or queued turn."""
        state = self._chats.get(chat_id) if chat_id is not None else None
        return state is not None and state.lock.locked()

    def agent_config(self) -> AgentConfig:
        # Fixed cwd so Claude Code stores/resumes sessions in one place.
        return replace(
            self.base,
            model=self.menu.prefs.model,
            effort=self.menu.effective_effort(),
            cwd=self.state_dir,
            plugin_dir=self.plugin.path,
            skills=self.plugin.skills,
        )

    def _is_current(self, agent: BrowsingAgent) -> bool:
        cfg = self.agent_config()
        return (agent.config.model, agent.config.effort) == (cfg.model, cfg.effort)

    def save_session(self, chat_id: int, session_id: str | None) -> None:
        if session_id:
            self.sessions.set(chat_id, session_id, self.fingerprint)

    async def retire_agent(self, state: ChatState, chat_id: int) -> None:
        """Close the chat's agent but keep its session, so the next message
        resumes the same conversation with the current options. Caller must
        hold the chat lock (or know it is free)."""
        agent, state.agent = state.agent, None
        if agent is None:
            return
        self.save_session(chat_id, agent.session_id)
        await agent.close()

    def new_agent(self, chat_id: int) -> tuple[BrowsingAgent, bool]:
        """Create the chat's agent. The flag is True if a saved session was
        dropped because the agent's instructions changed since it started."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.agent_config()
        resume = self.sessions.get(chat_id, self.fingerprint)
        stale = resume is None and self.sessions.has(chat_id)
        if stale:
            logger.info("Instructions changed; not resuming the old session for chat {}", chat_id)
            self.sessions.set(chat_id, None)
        return BrowsingAgent(cfg, resume=resume), stale

    async def ready_agent(self, state: ChatState, chat_id: int) -> tuple[BrowsingAgent, bool]:
        """The chat's agent with the current options, and whether a saved
        session was dropped (see :meth:`new_agent`). Caller holds the lock."""
        if state.agent is not None and not self._is_current(state.agent):
            # /model or /effort changed since this agent started: reconnect,
            # resuming the same session with the new options.
            await self.retire_agent(state, chat_id)
        if state.agent is not None:
            return state.agent, False
        state.agent, stale = self.new_agent(chat_id)
        return state.agent, stale

    async def reset(self, chat_id: int) -> None:
        """Interrupt the chat's turn, close its agent and forget its session."""
        state = self.chat(chat_id)
        state.epoch += 1
        if state.lock.locked() and state.agent is not None:
            with contextlib.suppress(Exception):
                await state.agent.interrupt()
        async with state.lock:
            if state.agent is not None:
                await state.agent.close()
                state.agent = None
            self.sessions.set(chat_id, None)

    async def close_all(self) -> None:
        for state in self._chats.values():
            if state.agent is not None:
                await state.agent.close()
