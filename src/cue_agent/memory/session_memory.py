"""Conversation session memory wrapping EAP's StateManager."""

from __future__ import annotations

import logging
from typing import Optional

from eap.protocol.state_manager import StateManager
from protocol.models import MemoryStrategy  # noqa: I001 — not yet re-exported via eap.protocol.models

logger = logging.getLogger(__name__)


class SessionMemory:
    """Per-chat conversation memory backed by EAP's StateManager."""

    def __init__(self, state_manager: StateManager, window_turn_limit: int = 20):
        self._sm = state_manager
        self._window_limit = window_turn_limit
        self._sessions: dict[str, str] = {}  # chat_id -> session_id

    def get_or_create_session(self, chat_id: str) -> str:
        """Get existing session or create a new one for this chat."""
        if chat_id not in self._sessions:
            session = self._sm.create_session(
                memory_strategy=MemoryStrategy.WINDOW,
                window_turn_limit=self._window_limit,
            )
            self._sessions[chat_id] = session["session_id"]
            logger.info("Created session %s for chat %s", session["session_id"], chat_id)
        return self._sessions[chat_id]

    def add_turn(
        self,
        chat_id: str,
        role: str,
        content: str,
        run_id: Optional[str] = None,
    ) -> None:
        """Append a conversation turn."""
        session_id = self.get_or_create_session(chat_id)
        self._sm.append_turn(
            session_id=session_id,
            role=role,
            content=content,
            macro_run_id=run_id,
        )

    def get_context(self, chat_id: str, limit: int = 20) -> str:
        """Build a conversation context string from recent turns."""
        session_id = self.get_or_create_session(chat_id)
        turns = self._sm.list_turns(session_id, limit=limit)
        if not turns:
            return ""
        return "\n".join(f"{t['role']}: {t['content']}" for t in turns)
