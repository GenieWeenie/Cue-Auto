"""Tests for multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from threading import Lock

import pytest

from cue_agent.orchestration.multi_agent import (
    SUB_AGENT_STATUS_COMPLETED,
    SUB_AGENT_STATUS_KILLED,
    SUB_AGENT_STATUS_TIMEOUT,
    MultiAgentOrchestrator,
    SubAgentSpec,
)


class _FakeMemory:
    def __init__(self):
        self.turns: list[dict[str, str]] = []

    def get_context(self, chat_id: str, limit: int = 20) -> str:  # noqa: ARG002
        return f"context:{chat_id}"

    def add_turn(self, chat_id: str, role: str, content: str, run_id: str | None = None) -> None:  # noqa: ARG002
        self.turns.append({"chat_id": chat_id, "role": role, "content": content})


class _FakeRouter:
    def __init__(self):
        self.selected: list[str] = []
        self._current = "auto"

    @contextmanager
    def provider_preference(self, provider_name: str):
        previous = self._current
        self._current = provider_name
        self.selected.append(provider_name)
        try:
            yield
        finally:
            self._current = previous

    @property
    def current(self) -> str:
        return self._current


class _FakeBrain:
    def __init__(self, router: _FakeRouter, *, delay_seconds: float, on_chat):
        self.router = router
        self.delay_seconds = delay_seconds
        self.on_chat = on_chat

    def chat(self, prompt: str, extra_context: str = "") -> str:
        del extra_context
        time.sleep(self.delay_seconds)
        self.on_chat()
        return f"{self.router.current}:{prompt[:20]}"


class _ConcurrencyBrain(_FakeBrain):
    def __init__(self, router: _FakeRouter, *, delay_seconds: float):
        super().__init__(router, delay_seconds=delay_seconds, on_chat=lambda: None)
        self._lock = Lock()
        self.active = 0
        self.max_seen = 0

    def chat(self, prompt: str, extra_context: str = "") -> str:
        del prompt, extra_context
        with self._lock:
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)
        time.sleep(self.delay_seconds)
        with self._lock:
            self.active -= 1
        return f"{self.router.current}:ok"


@pytest.mark.asyncio
async def test_multi_agent_orchestrator_runs_handoff_and_tracks_cost():
    memory = _FakeMemory()
    router = _FakeRouter()
    total_cost = {"value": 0.0}

    def _on_chat() -> None:
        total_cost["value"] += 0.25

    brain = _FakeBrain(router, delay_seconds=0.01, on_chat=_on_chat)
    orchestrator = MultiAgentOrchestrator(
        brain=brain,  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        max_concurrent=2,
        total_cost_provider=lambda: total_cost["value"],
        inherited_policies={"approval_required_levels": ["high", "critical"]},
    )

    results = await orchestrator.run_handoff(
        parent_task="Parent task",
        parent_agent_id="parent-1",
        specs=[
            SubAgentSpec(agent_id="agent-a", prompt="Collect logs", provider_preference="openai"),
            SubAgentSpec(agent_id="agent-b", prompt="Check backups", provider_preference="lmstudio"),
        ],
    )

    assert [result.status for result in results] == [SUB_AGENT_STATUS_COMPLETED, SUB_AGENT_STATUS_COMPLETED]
    assert router.selected == ["openai", "lmstudio"]
    assert len(memory.turns) == 4
    assert all(turn["chat_id"].startswith("sub-agent:parent-1:") for turn in memory.turns)

    snapshot = orchestrator.status_snapshot()
    assert snapshot["active_parents"] == 0
    assert snapshot["subagent_requests"] == 2
    assert float(snapshot["subagent_estimated_cost_usd"]) > 0.0
    assert len(snapshot["recent_parents"]) == 1


@pytest.mark.asyncio
async def test_multi_agent_orchestrator_timeout_and_kill():
    memory = _FakeMemory()
    router = _FakeRouter()
    brain = _FakeBrain(router, delay_seconds=0.2, on_chat=lambda: None)
    orchestrator = MultiAgentOrchestrator(
        brain=brain,  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        max_concurrent=1,
        default_timeout_seconds=1,
    )

    timeout_results = await orchestrator.run_handoff(
        parent_task="Parent",
        parent_agent_id="parent-timeout",
        specs=[SubAgentSpec(agent_id="timeout-agent", prompt="slow", timeout_seconds=0.01)],
    )
    assert timeout_results[0].status == SUB_AGENT_STATUS_TIMEOUT

    kill_task = asyncio.create_task(
        orchestrator.run_handoff(
            parent_task="Parent",
            parent_agent_id="parent-kill",
            specs=[SubAgentSpec(agent_id="kill-agent", prompt="slow", timeout_seconds=5)],
        )
    )
    await asyncio.sleep(0.05)
    assert orchestrator.kill_sub_agent("kill-agent") is True
    kill_results = await kill_task
    assert kill_results[0].status == SUB_AGENT_STATUS_KILLED


@pytest.mark.asyncio
async def test_multi_agent_orchestrator_respects_max_concurrency():
    memory = _FakeMemory()
    router = _FakeRouter()
    brain = _ConcurrencyBrain(router, delay_seconds=0.08)
    orchestrator = MultiAgentOrchestrator(
        brain=brain,  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        max_concurrent=1,
    )

    _ = await orchestrator.run_handoff(
        parent_task="Parent",
        parent_agent_id="parent-concurrency",
        specs=[
            SubAgentSpec(agent_id="a", prompt="one", timeout_seconds=2),
            SubAgentSpec(agent_id="b", prompt="two", timeout_seconds=2),
        ],
    )
    assert brain.max_seen == 1
