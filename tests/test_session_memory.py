"""Tests for SessionMemory."""

from __future__ import annotations

from cue_agent.memory.session_memory import SessionMemory


class _FakeStateManager:
    def __init__(self):
        self._session_id = "sess-1"
        self.append_calls: list[dict] = []
        self.turns: list[dict] = []
        self.create_count = 0

    def create_session(self, memory_strategy, window_turn_limit):
        del memory_strategy, window_turn_limit
        self.create_count += 1
        return {"session_id": self._session_id}

    def append_turn(self, session_id, role, content, macro_run_id=None):
        self.append_calls.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "macro_run_id": macro_run_id,
            }
        )
        self.turns.append({"role": role, "content": content})

    def list_turns(self, session_id, limit):
        del session_id, limit
        return list(self.turns)


def test_session_memory_reuses_session_per_chat():
    sm = _FakeStateManager()
    memory = SessionMemory(sm, window_turn_limit=5)

    session1 = memory.get_or_create_session("chat-a")
    session2 = memory.get_or_create_session("chat-a")

    assert session1 == "sess-1"
    assert session2 == "sess-1"
    assert sm.create_count == 1


def test_session_memory_add_turn_and_context():
    sm = _FakeStateManager()
    memory = SessionMemory(sm)

    memory.add_turn("chat-a", "user", "hello")
    memory.add_turn("chat-a", "assistant", "hi there", run_id="run-123")

    assert len(sm.append_calls) == 2
    assert sm.append_calls[1]["macro_run_id"] == "run-123"

    context = memory.get_context("chat-a")
    assert "user: hello" in context
    assert "assistant: hi there" in context
