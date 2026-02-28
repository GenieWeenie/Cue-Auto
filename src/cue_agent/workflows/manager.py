"""Workflow manager coordinating load/refresh, trigger lookup, and execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cue_agent.workflows.engine import WorkflowEngine, WorkflowRunResult
from cue_agent.workflows.loader import WorkflowDefinition, WorkflowLoader


@dataclass(frozen=True)
class ScheduledWorkflowTrigger:
    workflow_name: str
    cron: str
    trigger_id: str


class WorkflowManager:
    """Holds workflow definitions and runs them on demand."""

    def __init__(self, loader: WorkflowLoader, engine: WorkflowEngine):
        self._loader = loader
        self._engine = engine
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._fingerprint: dict[str, float] = {}
        self.reload_all()

    def reload_all(self) -> list[str]:
        self._workflows = self._loader.load_all()
        self._fingerprint = self._loader.fingerprint()
        return sorted(self._workflows.keys())

    def refresh_if_needed(self) -> bool:
        current = self._loader.fingerprint()
        if current == self._fingerprint:
            return False
        self.reload_all()
        return True

    @property
    def workflow_names(self) -> list[str]:
        return sorted(self._workflows.keys())

    def workflow(self, name: str) -> WorkflowDefinition | None:
        self.refresh_if_needed()
        return self._workflows.get(name.strip())

    def list_templates(self) -> list[str]:
        templates: list[str] = []
        for path in self._loader.template_files():
            templates.append(path.stem)
        return sorted(templates)

    def template_path(self, template_name: str) -> Path | None:
        normalized = template_name.strip().lower()
        for path in self._loader.template_files():
            if path.stem.lower() == normalized:
                return path
        return None

    def event_workflows(self, event_name: str) -> list[str]:
        self.refresh_if_needed()
        matched: list[str] = []
        for workflow in self._workflows.values():
            for event in workflow.trigger.events:
                if event == event_name:
                    matched.append(workflow.name)
                    break
                if event.endswith("*"):
                    prefix = event[:-1]
                    if event_name.startswith(prefix):
                        matched.append(workflow.name)
                        break
        return sorted(set(matched))

    def scheduled_triggers(self) -> list[ScheduledWorkflowTrigger]:
        self.refresh_if_needed()
        rows: list[ScheduledWorkflowTrigger] = []
        for workflow in self._workflows.values():
            for idx, cron in enumerate(workflow.trigger.schedules, start=1):
                safe_name = workflow.name.replace(" ", "_").replace("/", "_")
                rows.append(
                    ScheduledWorkflowTrigger(
                        workflow_name=workflow.name,
                        cron=cron,
                        trigger_id=f"workflow_{safe_name}_{idx}",
                    )
                )
        return rows

    async def run_workflow(
        self,
        name: str,
        *,
        trigger: str,
        input_text: str = "",
        event_payload: dict[str, Any] | None = None,
        actor_user_id: str = "",
    ) -> WorkflowRunResult:
        workflow = self.workflow(name)
        if workflow is None:
            raise ValueError(f"Unknown workflow: {name}")
        return await self._engine.run(
            workflow,
            trigger=trigger,
            input_text=input_text,
            event_payload=event_payload,
            actor_user_id=actor_user_id,
        )

    def fire_event(
        self,
        event_name: str,
        *,
        payload: dict[str, Any] | None = None,
        actor_user_id: str = "",
    ) -> list[asyncio.Task[WorkflowRunResult]]:
        matched = self.event_workflows(event_name)
        if not matched:
            return []
        tasks: list[asyncio.Task[WorkflowRunResult]] = []
        for workflow_name in matched:
            tasks.append(
                asyncio.create_task(
                    self.run_workflow(
                        workflow_name,
                        trigger=f"event:{event_name}",
                        event_payload=payload,
                        actor_user_id=actor_user_id,
                    ),
                    name=f"workflow-event:{workflow_name}",
                )
            )
        return tasks
