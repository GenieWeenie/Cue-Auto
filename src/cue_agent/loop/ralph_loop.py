"""Ralph-style autonomous outer loop: orient -> pick -> plan -> execute -> verify -> commit."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from environment.executor import AsyncLocalExecutor
from protocol.state_manager import StateManager

from cue_agent.actions.registry import ActionRegistry
from cue_agent.brain.cue_brain import CueBrain
from cue_agent.config import CueConfig
from cue_agent.logging_utils import correlation_context, new_correlation_id
from cue_agent.orchestration.multi_agent import (
    SUB_AGENT_STATUS_COMPLETED,
    MultiAgentOrchestrator,
    SubAgentSpec,
)
from cue_agent.loop.task_picker import TaskPicker
from cue_agent.loop.task_queue import TASK_STATUS_PENDING, TaskQueue
from cue_agent.loop.verifier import Verifier
from cue_agent.memory.session_memory import SessionMemory
from cue_agent.memory.vector_memory import VectorMemory
from cue_agent.retry_utils import backoff_delay_seconds
from cue_agent.security.approval_gate import ApprovalGate

logger = logging.getLogger(__name__)

LOOP_CHAT_ID = "system_loop"


class RalphLoop:
    """Autonomous agent loop. Each iteration has a fresh LLM context."""

    def __init__(
        self,
        brain: CueBrain,
        memory: SessionMemory,
        actions: ActionRegistry,
        executor: AsyncLocalExecutor,
        state_manager: StateManager,
        approval_gate: ApprovalGate,
        config: CueConfig,
        vector_memory: VectorMemory | None = None,
        task_queue: TaskQueue | None = None,
        notification_handler: Callable[[dict[str, Any]], None] | None = None,
        multi_agent_orchestrator: MultiAgentOrchestrator | None = None,
    ):
        self.brain = brain
        self.memory = memory
        self.vector_memory = vector_memory
        self.task_queue = task_queue
        self.actions = actions
        self.executor = executor
        self.state = state_manager
        self.approval_gate = approval_gate
        self.config = config
        self._notification_handler = notification_handler
        self._multi_agent_orchestrator = multi_agent_orchestrator
        self._task_picker = TaskPicker(brain)
        self._verifier = Verifier(brain)
        self._running = False
        self._iteration_count = 0
        self._last_iteration_time: datetime | None = None
        self._current_task: str | None = None
        self._consecutive_failures = 0

    async def run_forever(self) -> None:
        """Run the autonomous loop until stopped."""
        self._running = True
        logger.info("Ralph loop started (interval=%ds)", self.config.loop_interval_seconds)

        while self._running:
            try:
                if (
                    self.config.loop_cooldown_after_failures > 0
                    and self._consecutive_failures >= self.config.loop_cooldown_after_failures
                ):
                    logger.info(
                        "Loop cooldown: %d consecutive failures, sleeping %ds",
                        self._consecutive_failures,
                        self.config.loop_cooldown_seconds,
                    )
                    await asyncio.sleep(self.config.loop_cooldown_seconds)
                    self._consecutive_failures = 0
                await self._iteration()
                self._consecutive_failures = 0
            except Exception:
                logger.exception("Loop iteration %d failed", self._iteration_count)
                self._consecutive_failures += 1
            await asyncio.sleep(self.config.loop_interval_seconds)

    async def run_once(self) -> None:
        """Execute a single loop iteration."""
        if (
            self.config.loop_cooldown_after_failures > 0
            and self._consecutive_failures >= self.config.loop_cooldown_after_failures
        ):
            logger.info(
                "Loop cooldown: %d consecutive failures, sleeping %ds",
                self._consecutive_failures,
                self.config.loop_cooldown_seconds,
            )
            await asyncio.sleep(self.config.loop_cooldown_seconds)
            self._consecutive_failures = 0
        try:
            await self._iteration()
            self._consecutive_failures = 0
        except Exception:
            self._consecutive_failures += 1
            raise

    def stop(self) -> None:
        """Signal the loop to stop after the current iteration."""
        self._running = False
        logger.info("Ralph loop stop requested")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_iteration_time(self) -> str | None:
        if self._last_iteration_time is None:
            return None
        return self._last_iteration_time.isoformat()

    @property
    def current_task(self) -> str | None:
        return self._current_task

    async def _iteration(self) -> None:
        self._iteration_count += 1
        self._last_iteration_time = datetime.now(timezone.utc)
        corr_id = new_correlation_id(f"loop{self._iteration_count}")
        with correlation_context(corr_id):
            await self._run_iteration_body()

    async def _run_iteration_body(self) -> None:
        logger.info(
            "Loop iteration started",
            extra={
                "event": "loop_iteration_started",
                "iteration": self._iteration_count,
            },
        )

        # 1. ORIENT — read current state
        context = self._build_context()

        # 2. PICK — select next task
        selected_task_id: int | None = None
        task = None
        queued_task: dict[str, Any] | None = None
        if self.task_queue is not None and self.config.task_queue_enabled:
            queued = self.task_queue.next_unblocked_task()
            if queued is not None:
                queued_task = queued
                selected_task_id = int(queued["id"])
                task = str(queued["title"])
                description = str(queued.get("description", "")).strip()
                if description:
                    task = f"{task}\n\nContext: {description}"
                self.task_queue.mark_in_progress(selected_task_id)
                self._maybe_create_subtasks(queued)

        if task is None:
            task = self._task_picker.pick(context)
            if task is None:
                self._current_task = None
                logger.info("Idle — no tasks to execute")
                return

        self._current_task = str(task).split("\n", 1)[0][:240]
        run_id = f"loop_{self._iteration_count}_{uuid4().hex[:6]}"

        try:
            # 3. PLAN — generate EAP macro
            manifest = self.actions.get_hashed_manifest()
            memory_ctx = self.memory.get_context(LOOP_CHAT_ID, limit=10)
            delegated_context = await self._delegate_subtasks(
                selected_task_id=selected_task_id,
                queued_task=queued_task,
                parent_agent_id=f"loop-{self._iteration_count}",
            )
            if delegated_context:
                memory_ctx = f"{memory_ctx}\n\n{delegated_context}" if memory_ctx else delegated_context
            if self.vector_memory is not None:
                vector_ctx = self.vector_memory.recall_as_context(LOOP_CHAT_ID, task)
                if vector_ctx:
                    memory_ctx = f"{memory_ctx}\n\n{vector_ctx}" if memory_ctx else vector_ctx
            macro = self.brain.plan(task, manifest, memory_context=memory_ctx)
            logger.info("Generated macro with %d steps", len(macro.steps))

            # 4. APPROVE — inject HITL checkpoints
            macro = self.approval_gate.inject_approvals(macro)

            # 5. EXECUTE — run via EAP's AsyncLocalExecutor
            try:
                result = await self._execute_macro_with_retry(macro, run_id=run_id)
            except Exception as exc:
                if selected_task_id is not None and self.task_queue is not None:
                    self.task_queue.mark_failed(
                        selected_task_id,
                        error=str(exc),
                        retry_limit=self.config.task_queue_retry_failed_attempts,
                    )
                    self._emit_notification(
                        category="task_completion",
                        priority="high",
                        title=f"Task #{selected_task_id} failed",
                        body=str(exc),
                    )
                raise

            # 6. VERIFY — check success
            result_str = str(result) if result else "No output"
            verification = self._verifier.verify(task, result_str)
            if selected_task_id is not None and self.task_queue is not None:
                if verification.success:
                    self.task_queue.mark_done(selected_task_id)
                    self._emit_notification(
                        category="task_completion",
                        priority="medium",
                        title=f"Task #{selected_task_id} completed",
                        body=verification.summary,
                    )
                else:
                    self.task_queue.mark_failed(
                        selected_task_id,
                        error=verification.summary,
                        retry_limit=self.config.task_queue_retry_failed_attempts,
                    )
                    self._emit_notification(
                        category="task_completion",
                        priority="high",
                        title=f"Task #{selected_task_id} failed verification",
                        body=verification.summary,
                    )

            # 7. COMMIT — record in memory
            outcome = f"Task: {task}\nResult: {verification.summary}"
            self.memory.add_turn(LOOP_CHAT_ID, "assistant", outcome, run_id=run_id)
            if self.vector_memory is not None:
                self.vector_memory.add_turn(LOOP_CHAT_ID, "assistant", outcome, run_id=run_id)
            logger.info(
                "Loop iteration complete",
                extra={
                    "event": "loop_iteration_complete",
                    "iteration": self._iteration_count,
                    "run_id": run_id,
                    "success": verification.success,
                },
            )
        finally:
            self._current_task = None

    def _emit_notification(self, *, category: str, priority: str, title: str, body: str) -> None:
        if self._notification_handler is None:
            return
        self._notification_handler(
            {
                "event": category,
                "priority": priority,
                "title": title,
                "body": body,
                "source": "loop",
            }
        )

    async def _execute_macro_with_retry(self, macro: Any, run_id: str) -> Any:
        attempts = max(1, self.config.retry_tool_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await self.executor.execute_macro(macro, run_id=run_id)
            except Exception as exc:
                logger.warning(
                    "Macro execution failed",
                    extra={
                        "event": "tool_execution_retry",
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "error": str(exc),
                    },
                )
                if attempt >= attempts:
                    raise

                delay = backoff_delay_seconds(
                    attempt,
                    base_delay=self.config.retry_base_delay_seconds,
                    max_delay=self.config.retry_max_delay_seconds,
                    jitter=self.config.retry_jitter_seconds,
                )
                await asyncio.sleep(delay)

    def _build_context(self) -> str:
        """Assemble current state for task picking."""
        parts = [
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
            f"Iteration: {self._iteration_count}",
        ]

        # Recent conversation/activity
        recent = self.memory.get_context(LOOP_CHAT_ID, limit=5)
        if recent:
            parts.append(f"Recent activity:\n{recent}")
        else:
            parts.append("Recent activity: none")

        if self.task_queue is not None and self.config.task_queue_enabled:
            queued = self.task_queue.list_tasks(status=TASK_STATUS_PENDING, limit=5)
            if queued:
                queue_lines = "\n".join(f"- #{task['id']} p{task['priority']}: {task['title']}" for task in queued)
                parts.append(f"Queued tasks:\n{queue_lines}")
            else:
                parts.append("Queued tasks: none")

        return "\n\n".join(parts)

    def _maybe_create_subtasks(self, parent_task: dict[str, Any]) -> None:
        if self.task_queue is None:
            return
        if not self.config.task_queue_auto_subtasks_enabled:
            return

        parent_id = int(parent_task["id"])
        if self.task_queue.child_count(parent_id) > 0:
            return

        max_items = max(1, self.config.task_queue_auto_subtasks_max)
        prompt = (
            "Break the following task into actionable sub-tasks.\n"
            f"Return up to {max_items} lines, one per sub-task.\n"
            "Use bullet lines only. If no sub-tasks are needed, respond with NOTHING.\n\n"
            f"Task:\n{parent_task['title']}\n{parent_task.get('description', '')}"
        )
        response = self.brain.chat(prompt)
        subtasks = _parse_subtasks(response, max_items=max_items)
        parent_priority = int(parent_task.get("priority", 3))
        child_priority = min(4, parent_priority + 1)
        for subtask in subtasks:
            try:
                self.task_queue.create_subtask(
                    parent_task_id=parent_id,
                    title=subtask,
                    priority=child_priority,
                    source="agent_autosubtask",
                )
            except Exception:
                logger.exception(
                    "Failed to create auto sub-task",
                    extra={
                        "event": "task_queue_subtask_create_failed",
                        "parent_task_id": parent_id,
                    },
                )

    async def _delegate_subtasks(
        self,
        *,
        selected_task_id: int | None,
        queued_task: dict[str, Any] | None,
        parent_agent_id: str,
    ) -> str:
        if selected_task_id is None or queued_task is None:
            return ""
        if self.task_queue is None or self._multi_agent_orchestrator is None:
            return ""
        if not getattr(self.config, "multi_agent_enabled", True):
            return ""

        max_concurrent = max(1, int(getattr(self.config, "multi_agent_max_concurrent", 3)))
        timeout_seconds = max(1, int(getattr(self.config, "multi_agent_subagent_timeout_seconds", 120)))
        provider_preference = (
            str(getattr(self.config, "multi_agent_default_provider_preference", "auto")).strip() or "auto"
        )
        children = self.task_queue.list_child_tasks(
            parent_task_id=selected_task_id, status=TASK_STATUS_PENDING, limit=50
        )
        if not children:
            return ""

        specs: list[SubAgentSpec] = []
        for child in children:
            child_id = int(child["id"])
            title = str(child.get("title", "")).strip()
            if not title:
                continue
            description = str(child.get("description", "")).strip()
            self.task_queue.mark_in_progress(child_id)
            prompt_parts = [
                f"Sub-task #{child_id}: {title}",
                "Return concise findings and explicit next action(s).",
            ]
            if description:
                prompt_parts.append(f"Additional context: {description}")
            specs.append(
                SubAgentSpec(
                    agent_id=f"task-{child_id}",
                    prompt="\n".join(prompt_parts),
                    skill_scopes=("analysis", "execution"),
                    provider_preference=provider_preference,
                    timeout_seconds=timeout_seconds,
                    metadata={"task_id": child_id},
                )
            )

        if not specs:
            return ""

        specs = specs[: max(max_concurrent * 2, max_concurrent)]
        results = await self._multi_agent_orchestrator.run_handoff(
            parent_task=str(queued_task.get("title", "")),
            specs=specs,
            parent_agent_id=parent_agent_id,
        )
        if not results:
            return ""

        lines = ["Sub-agent handoff summary:"]
        for result in results:
            task_id_obj = result.metadata.get("task_id")
            child_task_id = int(task_id_obj) if isinstance(task_id_obj, int) else None
            if child_task_id is not None:
                if result.status == SUB_AGENT_STATUS_COMPLETED:
                    self.task_queue.mark_done(child_task_id)
                else:
                    message = result.error.strip() or f"sub-agent status={result.status}"
                    self.task_queue.mark_failed(
                        child_task_id,
                        error=message,
                        retry_limit=self.config.task_queue_retry_failed_attempts,
                    )
            summary = result.output.strip().splitlines()[0] if result.output.strip() else result.error.strip()
            if not summary:
                summary = result.status
            lines.append(f"- {result.agent_id}: {result.status} ({summary[:140]})")
        return "\n".join(lines)


def _parse_subtasks(text: str, max_items: int) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if "NOTHING" in stripped.upper():
        return []

    items: list[str] = []
    seen: set[str] = set()
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        elif "." in line and line.split(".", 1)[0].isdigit():
            line = line.split(".", 1)[1].strip()
        if not line:
            continue
        norm = line.lower()
        if norm in seen:
            continue
        seen.add(norm)
        items.append(line)
        if len(items) >= max_items:
            break
    return items
