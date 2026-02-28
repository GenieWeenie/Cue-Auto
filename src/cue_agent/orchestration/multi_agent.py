"""Multi-agent orchestration with queue-based result handoff."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from cue_agent.brain.cue_brain import CueBrain
from cue_agent.memory.session_memory import SessionMemory

SUB_AGENT_STATUS_QUEUED = "queued"
SUB_AGENT_STATUS_RUNNING = "running"
SUB_AGENT_STATUS_COMPLETED = "completed"
SUB_AGENT_STATUS_FAILED = "failed"
SUB_AGENT_STATUS_TIMEOUT = "timeout"
SUB_AGENT_STATUS_KILLED = "killed"


@dataclass(slots=True)
class SubAgentSpec:
    """Execution request for a sub-agent."""

    agent_id: str
    prompt: str
    skill_scopes: tuple[str, ...] = ()
    provider_preference: str = "auto"
    timeout_seconds: int = 120
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SubAgentResult:
    """Finalized sub-agent execution result."""

    agent_id: str
    parent_agent_id: str
    status: str
    output: str = ""
    error: str = ""
    started_at_utc: str = ""
    finished_at_utc: str = ""
    duration_ms: int = 0
    provider_preference: str = "auto"
    skill_scopes: tuple[str, ...] = ()
    estimated_cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "duration_ms": self.duration_ms,
            "provider_preference": self.provider_preference,
            "skill_scopes": list(self.skill_scopes),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class _SubAgentRuntime:
    spec: SubAgentSpec
    status: str = SUB_AGENT_STATUS_QUEUED
    started_at_utc: str = ""
    finished_at_utc: str = ""
    output_preview: str = ""
    error: str = ""
    estimated_cost_usd: float = 0.0
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ParentRuntime:
    parent_agent_id: str
    parent_task: str
    started_at_utc: str
    finished_at_utc: str = ""
    children: dict[str, _SubAgentRuntime] = field(default_factory=dict)


class MultiAgentOrchestrator:
    """Spawns and monitors sub-agents with bounded concurrency and result queue handoff."""

    def __init__(
        self,
        *,
        brain: CueBrain,
        memory: SessionMemory,
        max_concurrent: int = 3,
        default_timeout_seconds: int = 120,
        inherited_policies: dict[str, Any] | None = None,
        total_cost_provider: Callable[[], float] | None = None,
        max_parent_history: int = 10,
    ):
        self._brain = brain
        self._memory = memory
        self._max_concurrent = max(1, max_concurrent)
        self._default_timeout_seconds = max(1, default_timeout_seconds)
        self._inherited_policies = dict(inherited_policies or {})
        self._total_cost_provider = total_cost_provider
        self._max_parent_history = max(1, max_parent_history)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._active_parents: dict[str, _ParentRuntime] = {}
        self._recent_parents: list[dict[str, Any]] = []
        self._subagent_requests = 0
        self._subagent_estimated_cost_usd = 0.0

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    async def run_handoff(
        self,
        *,
        parent_task: str,
        specs: list[SubAgentSpec],
        parent_agent_id: str | None = None,
    ) -> list[SubAgentResult]:
        if not specs:
            return []
        parent_id = parent_agent_id.strip() if parent_agent_id else f"parent-{uuid4().hex[:8]}"
        now = _utcnow()
        runtime = _ParentRuntime(parent_agent_id=parent_id, parent_task=parent_task, started_at_utc=now)
        for spec in specs:
            runtime.children[spec.agent_id] = _SubAgentRuntime(spec=spec)
        self._active_parents[parent_id] = runtime

        result_queue: asyncio.Queue[SubAgentResult] = asyncio.Queue()
        ordered_ids = [spec.agent_id for spec in specs]
        for spec in specs:
            child = runtime.children[spec.agent_id]
            child.task = asyncio.create_task(
                self._run_sub_agent(parent_id, parent_task, child, result_queue),
                name=f"sub-agent:{parent_id}:{spec.agent_id}",
            )

        results_by_id: dict[str, SubAgentResult] = {}
        remaining = len(specs)
        while remaining > 0:
            result = await result_queue.get()
            results_by_id[result.agent_id] = result
            self._apply_result(runtime, result)
            remaining -= 1

        runtime.finished_at_utc = _utcnow()
        finished_snapshot = self._serialize_parent(runtime)
        self._recent_parents.append(finished_snapshot)
        if len(self._recent_parents) > self._max_parent_history:
            self._recent_parents = self._recent_parents[-self._max_parent_history :]
        self._active_parents.pop(parent_id, None)
        return [results_by_id[agent_id] for agent_id in ordered_ids if agent_id in results_by_id]

    def kill_sub_agent(self, agent_id: str) -> bool:
        target = agent_id.strip()
        if not target:
            return False
        for parent in self._active_parents.values():
            child = parent.children.get(target)
            if child is None:
                continue
            task = child.task
            if task is None or task.done():
                continue
            task.cancel()
            child.status = SUB_AGENT_STATUS_KILLED
            child.error = "killed by orchestrator"
            child.finished_at_utc = _utcnow()
            return True
        return False

    def status_snapshot(self) -> dict[str, Any]:
        active_sub_agents = sum(
            1
            for parent in self._active_parents.values()
            for child in parent.children.values()
            if child.status == SUB_AGENT_STATUS_RUNNING
        )
        return {
            "enabled": True,
            "max_concurrent": self._max_concurrent,
            "default_timeout_seconds": self._default_timeout_seconds,
            "inherited_policies": dict(self._inherited_policies),
            "active_parents": len(self._active_parents),
            "active_sub_agents": active_sub_agents,
            "subagent_requests": self._subagent_requests,
            "subagent_estimated_cost_usd": round(self._subagent_estimated_cost_usd, 6),
            "parents": [self._serialize_parent(parent) for parent in self._active_parents.values()],
            "recent_parents": list(self._recent_parents),
        }

    async def _run_sub_agent(
        self,
        parent_agent_id: str,
        parent_task: str,
        runtime: _SubAgentRuntime,
        result_queue: asyncio.Queue[SubAgentResult],
    ) -> None:
        spec = runtime.spec
        timeout_seconds = spec.timeout_seconds if spec.timeout_seconds > 0 else self._default_timeout_seconds
        memory_key = f"sub-agent:{parent_agent_id}:{spec.agent_id}"
        started_at = ""
        started_monotonic = time.monotonic()
        cost_before = 0.0
        output = ""
        error = ""
        status = SUB_AGENT_STATUS_COMPLETED
        try:
            async with self._semaphore:
                started_at = _utcnow()
                started_monotonic = time.monotonic()
                runtime.status = SUB_AGENT_STATUS_RUNNING
                runtime.started_at_utc = started_at
                cost_before = self._total_cost_usd()
                prompt = self._format_prompt(
                    parent_task=parent_task,
                    subtask_prompt=spec.prompt,
                    skill_scopes=spec.skill_scopes,
                )
                extra_context = self._memory.get_context(memory_key, limit=10)
                self._memory.add_turn(memory_key, "user", prompt)
                output = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._call_brain,
                        prompt,
                        extra_context,
                        spec.provider_preference,
                    ),
                    timeout=timeout_seconds,
                )
                self._memory.add_turn(memory_key, "assistant", output)
        except TimeoutError:
            status = SUB_AGENT_STATUS_TIMEOUT
            error = f"sub-agent timed out after {timeout_seconds}s"
        except asyncio.CancelledError:
            status = SUB_AGENT_STATUS_KILLED
            error = "sub-agent killed"
        except Exception as exc:  # pragma: no cover - defensive branch
            status = SUB_AGENT_STATUS_FAILED
            error = str(exc)

        finished_at = _utcnow()
        if not started_at:
            started_at = finished_at
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        estimated_delta = max(0.0, self._total_cost_usd() - cost_before)
        result = SubAgentResult(
            agent_id=spec.agent_id,
            parent_agent_id=parent_agent_id,
            status=status,
            output=output.strip(),
            error=error.strip(),
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            duration_ms=duration_ms,
            provider_preference=spec.provider_preference,
            skill_scopes=spec.skill_scopes,
            estimated_cost_usd=estimated_delta,
            metadata=dict(spec.metadata),
        )
        await result_queue.put(result)

    def _format_prompt(self, *, parent_task: str, subtask_prompt: str, skill_scopes: tuple[str, ...]) -> str:
        scope_text = ", ".join(skill_scopes) if skill_scopes else "default"
        required_levels = self._inherited_policies.get("approval_required_levels", [])
        policy_line = (
            f"Safety policy: high-risk operations require admin approval at levels {required_levels}."
            if isinstance(required_levels, list)
            else "Safety policy: inherit parent approval requirements."
        )
        return (
            "You are a delegated sub-agent.\n"
            f"Parent objective: {parent_task}\n"
            f"Scoped skills: {scope_text}\n"
            f"{policy_line}\n\n"
            "Sub-task:\n"
            f"{subtask_prompt.strip()}"
        )

    def _call_brain(self, prompt: str, extra_context: str, provider_preference: str) -> str:
        router = getattr(self._brain, "router", None)
        preference = provider_preference.strip().lower()
        manager = getattr(router, "provider_preference", None)
        if callable(manager) and preference and preference != "auto":
            with manager(preference):
                return str(self._brain.chat(prompt, extra_context=extra_context))
        return str(self._brain.chat(prompt, extra_context=extra_context))

    def _apply_result(self, parent: _ParentRuntime, result: SubAgentResult) -> None:
        child = parent.children.get(result.agent_id)
        if child is None:
            return
        child.status = result.status
        child.output_preview = result.output[:240]
        child.error = result.error[:240]
        child.finished_at_utc = result.finished_at_utc
        child.estimated_cost_usd = result.estimated_cost_usd
        self._subagent_requests += 1
        self._subagent_estimated_cost_usd += max(0.0, result.estimated_cost_usd)

    def _serialize_parent(self, runtime: _ParentRuntime) -> dict[str, Any]:
        children = []
        for child in runtime.children.values():
            children.append(
                {
                    "agent_id": child.spec.agent_id,
                    "status": child.status,
                    "provider_preference": child.spec.provider_preference,
                    "skill_scopes": list(child.spec.skill_scopes),
                    "started_at_utc": child.started_at_utc,
                    "finished_at_utc": child.finished_at_utc,
                    "output_preview": child.output_preview,
                    "error": child.error,
                    "estimated_cost_usd": round(child.estimated_cost_usd, 6),
                    "metadata": dict(child.spec.metadata),
                }
            )
        overall_status = "running"
        if runtime.finished_at_utc:
            overall_status = "completed"
        return {
            "parent_agent_id": runtime.parent_agent_id,
            "parent_task": runtime.parent_task,
            "status": overall_status,
            "started_at_utc": runtime.started_at_utc,
            "finished_at_utc": runtime.finished_at_utc,
            "sub_agents": children,
        }

    def _total_cost_usd(self) -> float:
        if self._total_cost_provider is None:
            return 0.0
        try:
            return float(self._total_cost_provider())
        except Exception:
            return 0.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
