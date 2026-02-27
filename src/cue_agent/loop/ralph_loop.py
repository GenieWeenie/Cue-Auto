"""Ralph-style autonomous outer loop: orient -> pick -> plan -> execute -> verify -> commit."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from environment.executor import AsyncLocalExecutor
from protocol.state_manager import StateManager

from cue_agent.actions.registry import ActionRegistry
from cue_agent.brain.cue_brain import CueBrain
from cue_agent.config import CueConfig
from cue_agent.loop.task_picker import TaskPicker
from cue_agent.loop.verifier import Verifier
from cue_agent.memory.session_memory import SessionMemory
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
    ):
        self.brain = brain
        self.memory = memory
        self.actions = actions
        self.executor = executor
        self.state = state_manager
        self.approval_gate = approval_gate
        self.config = config
        self._task_picker = TaskPicker(brain)
        self._verifier = Verifier(brain)
        self._running = False
        self._iteration_count = 0

    async def run_forever(self) -> None:
        """Run the autonomous loop until stopped."""
        self._running = True
        logger.info("Ralph loop started (interval=%ds)", self.config.loop_interval_seconds)

        while self._running:
            try:
                await self._iteration()
            except Exception:
                logger.exception("Loop iteration %d failed", self._iteration_count)
            await asyncio.sleep(self.config.loop_interval_seconds)

    async def run_once(self) -> None:
        """Execute a single loop iteration."""
        await self._iteration()

    def stop(self) -> None:
        """Signal the loop to stop after the current iteration."""
        self._running = False
        logger.info("Ralph loop stop requested")

    async def _iteration(self) -> None:
        self._iteration_count += 1
        logger.info("=== Loop iteration %d ===", self._iteration_count)

        # 1. ORIENT — read current state
        context = self._build_context()

        # 2. PICK — select next task
        task = self._task_picker.pick(context)
        if task is None:
            logger.info("Idle — no tasks to execute")
            return

        # 3. PLAN — generate EAP macro
        manifest = self.actions.get_hashed_manifest()
        memory_ctx = self.memory.get_context(LOOP_CHAT_ID, limit=10)
        macro = self.brain.plan(task, manifest, memory_context=memory_ctx)
        logger.info("Generated macro with %d steps", len(macro.steps))

        # 4. APPROVE — inject HITL checkpoints
        macro = self.approval_gate.inject_approvals(macro)

        # 5. EXECUTE — run via EAP's AsyncLocalExecutor
        run_id = f"loop_{self._iteration_count}_{uuid4().hex[:6]}"
        result = await self.executor.execute_macro(macro, run_id=run_id)

        # 6. VERIFY — check success
        result_str = str(result) if result else "No output"
        verification = self._verifier.verify(task, result_str)

        # 7. COMMIT — record in memory
        outcome = f"Task: {task}\nResult: {verification.summary}"
        self.memory.add_turn(LOOP_CHAT_ID, "assistant", outcome, run_id=run_id)
        logger.info(
            "Iteration %d complete: %s",
            self._iteration_count,
            "SUCCESS" if verification.success else "FAILURE",
        )

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

        return "\n\n".join(parts)
