"""Tests for loop components (TaskPicker, Verifier, RalphLoop)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cue_agent.config import CueConfig
from cue_agent.loop.ralph_loop import LOOP_CHAT_ID, RalphLoop
from cue_agent.loop.task_picker import TaskPicker
from cue_agent.loop.verifier import Verifier


class _FakeBrain:
    def __init__(self, chat_responses: list[str], macro_steps: int = 1):
        self.chat_responses = list(chat_responses)
        self.macro_steps = macro_steps
        self.plan_calls: list[dict] = []

    def chat(self, prompt: str, extra_context: str | None = None) -> str:
        del prompt, extra_context
        return self.chat_responses.pop(0)

    def plan(self, task: str, manifest: dict, memory_context: str):
        self.plan_calls.append({"task": task, "manifest": manifest, "memory_context": memory_context})
        return SimpleNamespace(steps=[SimpleNamespace(step_id=f"s{i}") for i in range(self.macro_steps)])


class _FakeMemory:
    def __init__(self):
        self.added_turns: list[dict] = []
        self.context_map = {LOOP_CHAT_ID: ""}

    def get_context(self, chat_id: str, limit: int = 20) -> str:
        del limit
        return self.context_map.get(chat_id, "")

    def add_turn(self, chat_id: str, role: str, content: str, run_id: str | None = None) -> None:
        self.added_turns.append({"chat_id": chat_id, "role": role, "content": content, "run_id": run_id})


class _FakeActions:
    def get_hashed_manifest(self):
        return {"tool_hash": "read_file"}


class _FakeExecutor:
    def __init__(self):
        self.calls: list[dict] = []
        self.failures_remaining = 0

    async def execute_macro(self, macro, run_id: str):
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("tool execution failed")
        self.calls.append({"macro": macro, "run_id": run_id})
        return {"ok": True}


class _FakeApprovalGate:
    def inject_approvals(self, macro):
        return macro


def test_task_picker_returns_none_on_nothing():
    picker = TaskPicker(_FakeBrain(["NOTHING"]))
    assert picker.pick("status context") is None


def test_verifier_parses_success_and_failure():
    success_verifier = Verifier(_FakeBrain(["SUCCESS - completed"]))
    failure_verifier = Verifier(_FakeBrain(["FAILURE - not completed"]))

    assert success_verifier.verify("t", "r").success is True
    assert failure_verifier.verify("t", "r").success is False


@pytest.mark.asyncio
async def test_ralph_loop_iteration_executes_task():
    brain = _FakeBrain(["Run backup", "SUCCESS - backup complete"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(loop_interval_seconds=1),
    )

    await loop.run_once()

    assert len(brain.plan_calls) == 1
    assert len(executor.calls) == 1
    assert len(memory.added_turns) == 1
    assert memory.added_turns[0]["chat_id"] == LOOP_CHAT_ID
    assert "Run backup" in memory.added_turns[0]["content"]
    assert loop.last_iteration_time is not None
    assert loop.is_running is False


@pytest.mark.asyncio
async def test_ralph_loop_iteration_idles_when_no_task():
    brain = _FakeBrain(["NOTHING"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(loop_interval_seconds=1),
    )

    await loop.run_once()

    assert len(brain.plan_calls) == 0
    assert len(executor.calls) == 0
    assert len(memory.added_turns) == 0


@pytest.mark.asyncio
async def test_ralph_loop_retries_macro_execution():
    brain = _FakeBrain(["Run backup", "SUCCESS - backup complete"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    executor.failures_remaining = 2
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(
            retry_tool_attempts=3,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            retry_jitter_seconds=0.0,
        ),
    )

    await loop.run_once()

    assert executor.failures_remaining == 0
    assert len(executor.calls) == 1
