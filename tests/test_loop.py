"""Tests for loop components (TaskPicker, Verifier, RalphLoop)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

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


class _FakeVectorMemory:
    def __init__(self):
        self.recalls: list[dict] = []
        self.added_turns: list[dict] = []

    def recall_as_context(self, chat_id: str, query: str, limit: int | None = None) -> str:
        self.recalls.append({"chat_id": chat_id, "query": query, "limit": limit})
        return "Long-term semantic memory:\n- prior loop outcome"

    def add_turn(self, chat_id: str, role: str, content: str, run_id: str | None = None) -> None:
        self.added_turns.append({"chat_id": chat_id, "role": role, "content": content, "run_id": run_id})


class _FakeTaskQueue:
    def __init__(self):
        self._next_task: dict | None = None
        self._children: list[dict] = []
        self.marked_in_progress: list[int] = []
        self.marked_done: list[int] = []
        self.marked_failed: list[dict] = []
        self.created_subtasks: list[dict] = []

    def next_unblocked_task(self):
        return self._next_task

    def list_tasks(self, status: str | None = None, limit: int = 20):  # noqa: ARG002
        if self._next_task is None:
            return []
        return [self._next_task]

    def list_child_tasks(self, parent_task_id: int, status: str | None = None, limit: int = 20):  # noqa: ARG002
        rows = [row for row in self._children if row["parent_task_id"] == parent_task_id]
        if status is not None:
            rows = [row for row in rows if row["status"] == status]
        return list(rows)

    def mark_in_progress(self, task_id: int) -> None:
        self.marked_in_progress.append(task_id)

    def mark_done(self, task_id: int) -> None:
        self.marked_done.append(task_id)

    def mark_failed(self, task_id: int, error: str, retry_limit: int) -> str:
        self.marked_failed.append({"task_id": task_id, "error": error, "retry_limit": retry_limit})
        return "failed"

    def child_count(self, parent_task_id: int) -> int:  # noqa: ARG002
        return 0

    def create_subtask(self, parent_task_id: int, title: str, priority: int, source: str):  # noqa: ARG002
        self.created_subtasks.append(
            {
                "parent_task_id": parent_task_id,
                "title": title,
                "priority": priority,
                "source": source,
            }
        )
        return 100


def test_task_picker_returns_none_on_nothing():
    picker = TaskPicker(_FakeBrain(["NOTHING"]))
    assert picker.pick("status context") is None


def test_verifier_parses_success_and_failure():
    success_verifier = Verifier(_FakeBrain(["SUCCESS - completed"]))
    failure_verifier = Verifier(_FakeBrain(["FAILURE - not completed"]))

    assert success_verifier.verify("t", "r").success is True
    assert failure_verifier.verify("t", "r").success is False


class _FakeOrchestrator:
    def __init__(self):
        self.calls: list[dict] = []

    async def run_handoff(self, *, parent_task: str, specs: list, parent_agent_id: str):
        self.calls.append({"parent_task": parent_task, "spec_count": len(specs), "parent_agent_id": parent_agent_id})
        if not specs:
            return []
        first = specs[0]
        task_id = int(first.metadata["task_id"])
        return [
            SimpleNamespace(
                agent_id=first.agent_id,
                status="completed",
                output="delegated done",
                error="",
                metadata={"task_id": task_id},
            )
        ]


@pytest.mark.asyncio
async def test_ralph_loop_iteration_executes_task():
    brain = _FakeBrain(["Run backup", "SUCCESS - backup complete"])
    memory = _FakeMemory()
    vector_memory = _FakeVectorMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        vector_memory=vector_memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(loop_interval_seconds=1),
    )

    await loop.run_once()

    assert len(brain.plan_calls) == 1
    assert "Long-term semantic memory" in brain.plan_calls[0]["memory_context"]
    assert len(vector_memory.recalls) == 1
    assert len(executor.calls) == 1
    assert len(memory.added_turns) == 1
    assert len(vector_memory.added_turns) == 1
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


@pytest.mark.asyncio
async def test_ralph_loop_uses_task_queue_and_marks_done():
    brain = _FakeBrain(["SUCCESS - backup complete"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    task_queue = _FakeTaskQueue()
    task_queue._next_task = {
        "id": 7,
        "title": "Queue backup",
        "description": "Nightly archive",
        "priority": 1,
    }
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(task_queue_enabled=True, task_queue_auto_subtasks_enabled=False),
        task_queue=task_queue,
    )

    await loop.run_once()

    assert task_queue.marked_in_progress == [7]
    assert task_queue.marked_done == [7]
    assert task_queue.marked_failed == []
    assert brain.plan_calls[0]["task"].startswith("Queue backup")


@pytest.mark.asyncio
async def test_ralph_loop_emits_task_notifications():
    brain = _FakeBrain(["SUCCESS - completed"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    task_queue = _FakeTaskQueue()
    task_queue._next_task = {
        "id": 11,
        "title": "Queue operation",
        "description": "",
        "priority": 2,
    }
    events: list[dict] = []
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(task_queue_enabled=True, task_queue_auto_subtasks_enabled=False),
        task_queue=task_queue,
        notification_handler=lambda event: events.append(event),
    )

    await loop.run_once()

    assert len(events) == 1
    assert events[0]["event"] == "task_completion"
    assert events[0]["priority"] == "medium"


@pytest.mark.asyncio
async def test_ralph_loop_creates_agent_subtasks_from_queue_item():
    brain = _FakeBrain(["- Collect logs\n- Validate backups", "SUCCESS - done"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    task_queue = _FakeTaskQueue()
    task_queue._next_task = {
        "id": 9,
        "title": "Investigate nightly backup issue",
        "description": "",
        "priority": 2,
    }
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(
            task_queue_enabled=True, task_queue_auto_subtasks_enabled=True, task_queue_auto_subtasks_max=3
        ),
        task_queue=task_queue,
    )

    await loop.run_once()

    assert len(task_queue.created_subtasks) == 2
    assert task_queue.created_subtasks[0]["parent_task_id"] == 9
    assert task_queue.created_subtasks[0]["source"] == "agent_autosubtask"


@pytest.mark.asyncio
async def test_ralph_loop_delegates_child_subtasks_to_multi_agent_orchestrator():
    brain = _FakeBrain(["SUCCESS - done"])
    memory = _FakeMemory()
    actions = _FakeActions()
    executor = _FakeExecutor()
    task_queue = _FakeTaskQueue()
    task_queue._next_task = {
        "id": 12,
        "title": "Parent issue",
        "description": "Parent description",
        "priority": 2,
    }
    task_queue._children = [
        {
            "id": 13,
            "title": "Child task",
            "description": "Investigate logs",
            "priority": 3,
            "status": "pending",
            "parent_task_id": 12,
        }
    ]
    orchestrator = _FakeOrchestrator()
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=CueConfig(
            task_queue_enabled=True,
            task_queue_auto_subtasks_enabled=False,
            multi_agent_enabled=True,
            multi_agent_max_concurrent=3,
            multi_agent_subagent_timeout_seconds=60,
            multi_agent_default_provider_preference="openai",
        ),
        task_queue=task_queue,
        multi_agent_orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert len(orchestrator.calls) == 1
    assert task_queue.marked_done == [13, 12]
    assert "Sub-agent handoff summary" in brain.plan_calls[0]["memory_context"]


@pytest.mark.asyncio
async def test_ralph_loop_cooldown_after_failures():
    """After N consecutive failures, loop sleeps cooldown_seconds before next iteration."""
    # Each run_once may call brain.chat (e.g. task_picker.pick if no queued task) and brain.plan; provide enough responses.
    brain = _FakeBrain(
        ["Run backup", "SUCCESS - backup complete", "Run backup", "SUCCESS - done"],
        macro_steps=1,
    )
    memory = _FakeMemory()
    memory.context_map[LOOP_CHAT_ID] = "prior"
    actions = _FakeActions()
    executor = _FakeExecutor()
    executor.failures_remaining = 999  # always fail (no retries in test)
    config = CueConfig(
        loop_cooldown_after_failures=2,
        loop_cooldown_seconds=0,  # 0 for fast test; we only assert sleep was called
        retry_tool_attempts=1,
    )
    loop = RalphLoop(
        brain=brain,
        memory=memory,
        actions=actions,
        executor=executor,
        state_manager=object(),
        approval_gate=_FakeApprovalGate(),
        config=config,
    )
    task_queue = _FakeTaskQueue()
    task_queue._next_task = {"id": 1, "title": "Task", "description": "", "priority": 1}
    loop.task_queue = task_queue

    sleep_calls: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("cue_agent.loop.ralph_loop.asyncio.sleep", side_effect=record_sleep):
        # First run: fails -> consecutive_failures=1
        with pytest.raises(RuntimeError, match="tool execution failed"):
            await loop.run_once()
        # Second run: fails -> consecutive_failures=2
        with pytest.raises(RuntimeError, match="tool execution failed"):
            await loop.run_once()
        # Third run: cooldown triggers (2 >= 2), sleep(0), then _iteration() runs and fails again
        with pytest.raises(RuntimeError, match="tool execution failed"):
            await loop.run_once()

    # Cooldown sleep should have been called on the third run_once (with 0 seconds)
    assert any(c == 0 for c in sleep_calls), "Expected asyncio.sleep(0) from cooldown"
