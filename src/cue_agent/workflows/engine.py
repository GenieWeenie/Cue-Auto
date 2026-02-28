"""Workflow execution engine with interpolation, branching, and parallel groups."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from cue_agent.actions.registry import ActionRegistry
from cue_agent.brain.cue_brain import CueBrain
from cue_agent.comms.approval_gateway import ApprovalGateway
from cue_agent.retry_utils import backoff_delay_seconds
from cue_agent.security.risk_classifier import RiskClassifier
from cue_agent.workflows.loader import WorkflowDefinition

_TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


@dataclass
class WorkflowStepResult:
    step_id: str
    step_type: str
    status: str
    started_at_utc: str
    finished_at_utc: str
    duration_ms: int
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "duration_ms": self.duration_ms,
            "output": dict(self.output),
            "error": self.error,
        }


@dataclass
class WorkflowRunResult:
    workflow_name: str
    trigger: str
    status: str
    started_at_utc: str
    finished_at_utc: str
    duration_ms: int
    step_results: list[WorkflowStepResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "trigger": self.trigger,
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "duration_ms": self.duration_ms,
            "step_results": [row.to_dict() for row in self.step_results],
        }


class WorkflowEngine:
    """Executes workflow definitions with step-level result tracking."""

    def __init__(
        self,
        *,
        brain: CueBrain,
        actions: ActionRegistry,
        risk_classifier: RiskClassifier,
        approval_gateway: ApprovalGateway | None,
        notification_handler: Callable[[dict[str, Any]], None] | None,
        retry_base_delay_seconds: float = 0.5,
        retry_max_delay_seconds: float = 5.0,
        retry_jitter_seconds: float = 0.2,
        audit_handler: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._brain = brain
        self._actions = actions
        self._risk_classifier = risk_classifier
        self._approval_gateway = approval_gateway
        self._notification_handler = notification_handler
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        self._retry_jitter_seconds = retry_jitter_seconds
        self._audit_handler = audit_handler

    async def run(
        self,
        workflow: WorkflowDefinition,
        *,
        trigger: str,
        input_text: str = "",
        event_payload: dict[str, Any] | None = None,
        actor_user_id: str = "",
    ) -> WorkflowRunResult:
        started_at = _utcnow()
        started_mono = time.monotonic()
        context: dict[str, Any] = {
            "input": {"text": input_text, "actor_user_id": actor_user_id},
            "event": event_payload or {},
            "steps": {},
        }
        step_results: list[WorkflowStepResult] = []
        workflow_status = "success"
        for step in workflow.steps:
            result = await self._execute_step(workflow, step, context)
            step_results.append(result)
            context["steps"][result.step_id] = {
                "status": result.status,
                "output": result.output,
                "error": result.error,
            }
            if result.status != "success":
                workflow_status = "failed"
                if not bool(step.get("continue_on_error", False)):
                    break

        finished_at = _utcnow()
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        return WorkflowRunResult(
            workflow_name=workflow.name,
            trigger=trigger,
            status=workflow_status,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            duration_ms=duration_ms,
            step_results=step_results,
        )

    async def _execute_step(
        self,
        workflow: WorkflowDefinition,
        step: dict[str, Any],
        context: dict[str, Any],
    ) -> WorkflowStepResult:
        step_id = str(step.get("id", "step"))
        step_type = str(step.get("type", "llm"))
        started_at = _utcnow()
        started_mono = time.monotonic()
        status = "success"
        output: dict[str, Any] = {}
        error = ""
        try:
            when_expr = str(step.get("when", "")).strip()
            if when_expr and not self._evaluate_condition(when_expr, context):
                output = {"skipped": True, "reason": "when=false"}
            elif step_type == "llm":
                output = await self._run_llm_step(step, context)
            elif step_type == "tool":
                output = await self._run_tool_step(step, context, step_id=step_id)
            elif step_type == "notification":
                output = self._run_notification_step(step, context)
            elif step_type == "approval":
                output = await self._run_approval_step(step, context, step_id=step_id)
            elif step_type == "condition":
                output = await self._run_condition_step(workflow, step, context)
            elif step_type == "parallel":
                output = await self._run_parallel_step(workflow, step, context)
            else:
                raise ValueError(f"Unsupported workflow step type: {step_type}")
        except Exception as exc:
            status = "failed"
            error = str(exc)

        finished_at = _utcnow()
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        result = WorkflowStepResult(
            step_id=step_id,
            step_type=step_type,
            status=status,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            duration_ms=duration_ms,
            output=output,
            error=error,
        )
        self._emit_step_audit(workflow, result)
        return result

    async def _run_llm_step(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        prompt = self._interpolate_value(str(step.get("prompt", "")), context)
        extra_context = self._interpolate_value(str(step.get("extra_context", "")), context)
        if not isinstance(prompt, str):
            prompt = str(prompt)
        if not isinstance(extra_context, str):
            extra_context = str(extra_context)
        text = await asyncio.to_thread(self._brain.chat, prompt, extra_context)
        return {"text": text}

    async def _run_tool_step(self, step: dict[str, Any], context: dict[str, Any], *, step_id: str) -> dict[str, Any]:
        tool_name = str(step.get("tool", "")).strip()
        if not tool_name:
            raise ValueError("tool step requires `tool` field")
        args_raw = step.get("arguments", {})
        if not isinstance(args_raw, dict):
            raise ValueError("tool step `arguments` must be a mapping")
        args = self._interpolate_value(args_raw, context)
        if not isinstance(args, dict):
            raise ValueError("tool step arguments interpolation produced invalid mapping")

        decision = self._risk_classifier.assess(
            tool_name,
            arguments=args,
            execution_context={"source": "workflow", "step_id": step_id},
        )
        if decision.requires_approval:
            if self._approval_gateway is None:
                raise RuntimeError(f"approval required for `{tool_name}` but gateway is not configured")
            approved = await self._approval_gateway.request_approval(
                f"Workflow tool step `{step_id}` approval required for `{tool_name}`: {decision.reason}",
                f"workflow_{step_id}",
            )
            if not approved:
                raise RuntimeError(f"approval denied for tool `{tool_name}`")

        fn = self._actions.eap_registry._tools.get(tool_name)
        if fn is None:
            raise ValueError(f"unknown tool: {tool_name}")

        retries = max(1, int(step.get("retry_attempts", 1)))
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if inspect.iscoroutinefunction(fn):
                    value = await fn(**args)
                else:
                    maybe_value = fn(**args)
                    if inspect.isawaitable(maybe_value):
                        value = await maybe_value
                    else:
                        value = maybe_value
                if isinstance(value, dict):
                    return {"result": value}
                return {"result": {"value": str(value)}}
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    raise
                await asyncio.sleep(
                    backoff_delay_seconds(
                        attempt,
                        base_delay=self._retry_base_delay_seconds,
                        max_delay=self._retry_max_delay_seconds,
                        jitter=self._retry_jitter_seconds,
                    )
                )
        if last_error is not None:
            raise last_error
        raise RuntimeError("tool step failed")  # pragma: no cover

    def _run_notification_step(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        title = str(self._interpolate_value(str(step.get("title", "Workflow Notification")), context))
        body = str(self._interpolate_value(str(step.get("body", "")), context))
        priority = str(step.get("priority", "medium")).strip().lower() or "medium"
        category = str(step.get("category", "workflow")).strip() or "workflow"
        if self._notification_handler is not None:
            self._notification_handler(
                {
                    "category": category,
                    "priority": priority,
                    "title": title,
                    "body": body,
                    "metadata": {"step_type": "notification"},
                }
            )
        return {"sent": self._notification_handler is not None}

    async def _run_approval_step(
        self,
        step: dict[str, Any],
        context: dict[str, Any],
        *,
        step_id: str,
    ) -> dict[str, Any]:
        if self._approval_gateway is None:
            raise RuntimeError("approval step requires configured approval gateway")
        prompt = str(self._interpolate_value(str(step.get("prompt", "Workflow approval required")), context))
        approved = await self._approval_gateway.request_approval(prompt, f"workflow_{step_id}")
        if not approved:
            raise RuntimeError(f"approval denied for step `{step_id}`")
        return {"approved": True}

    async def _run_condition_step(
        self,
        workflow: WorkflowDefinition,
        step: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        expression = str(step.get("expression", "")).strip()
        if not expression:
            raise ValueError("condition step requires `expression`")
        matched = self._evaluate_condition(expression, context)
        branch_key = "if_steps" if matched else "else_steps"
        branch_steps_raw = step.get(branch_key, [])
        if not isinstance(branch_steps_raw, list):
            raise ValueError(f"condition step `{branch_key}` must be a list")
        branch_results: list[dict[str, Any]] = []
        for child in branch_steps_raw:
            if not isinstance(child, dict):
                raise ValueError("nested condition steps must be mappings")
            child_result = await self._execute_step(workflow, child, context)
            context["steps"][child_result.step_id] = {
                "status": child_result.status,
                "output": child_result.output,
                "error": child_result.error,
            }
            branch_results.append(child_result.to_dict())
            if child_result.status != "success":
                break
        return {"matched": matched, "branch": branch_key, "steps": branch_results}

    async def _run_parallel_step(
        self,
        workflow: WorkflowDefinition,
        step: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        branches_raw = step.get("branches", [])
        if not isinstance(branches_raw, list) or not branches_raw:
            raise ValueError("parallel step requires non-empty `branches` list")

        tasks: list[asyncio.Task[dict[str, Any]]] = []
        for idx, branch in enumerate(branches_raw, start=1):
            if not isinstance(branch, dict):
                raise ValueError("parallel branch must be a mapping")
            branch_id = str(branch.get("id", f"branch_{idx}")).strip() or f"branch_{idx}"
            branch_steps_raw = branch.get("steps", [])
            if not isinstance(branch_steps_raw, list):
                raise ValueError("parallel branch `steps` must be a list")
            tasks.append(
                asyncio.create_task(
                    self._run_parallel_branch(
                        workflow,
                        branch_id=branch_id,
                        branch_steps=branch_steps_raw,
                        context=dict(context),
                    )
                )
            )
        results = await asyncio.gather(*tasks)
        aggregate: dict[str, Any] = {}
        for row in results:
            aggregate[row["branch_id"]] = row
        return {"branches": aggregate}

    async def _run_parallel_branch(
        self,
        workflow: WorkflowDefinition,
        *,
        branch_id: str,
        branch_steps: list[Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        branch_results: list[dict[str, Any]] = []
        status = "success"
        for child in branch_steps:
            if not isinstance(child, dict):
                raise ValueError("parallel branch step must be a mapping")
            child_result = await self._execute_step(workflow, child, context)
            context["steps"][child_result.step_id] = {
                "status": child_result.status,
                "output": child_result.output,
                "error": child_result.error,
            }
            branch_results.append(child_result.to_dict())
            if child_result.status != "success":
                status = "failed"
                break
        return {"branch_id": branch_id, "status": status, "steps": branch_results}

    def _interpolate_value(self, value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return self._interpolate_string(value, context)
        if isinstance(value, list):
            return [self._interpolate_value(item, context) for item in value]
        if isinstance(value, dict):
            return {key: self._interpolate_value(item, context) for key, item in value.items()}
        return value

    def _interpolate_string(self, text: str, context: dict[str, Any]) -> str:
        def _replace(match: re.Match[str]) -> str:
            path = match.group(1).strip()
            value = self._resolve_path(path, context)
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                return str(value)
            return str(value)

        return _TOKEN_PATTERN.sub(_replace, text)

    def _resolve_path(self, path: str, context: dict[str, Any]) -> Any:
        parts = [piece for piece in path.split(".") if piece]
        if not parts:
            return None

        if parts[0] in context:
            value: Any = context[parts[0]]
            for part in parts[1:]:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return None
            return value

        steps = context.get("steps", {})
        if isinstance(steps, dict) and parts[0] in steps:
            value = steps.get(parts[0])
            for part in parts[1:]:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return None
            return value
        return None

    def _evaluate_condition(self, expression: str, context: dict[str, Any]) -> bool:
        rendered = self._interpolate_string(expression, context).strip()
        if "==" in rendered:
            left, right = rendered.split("==", 1)
            return left.strip() == right.strip()
        if "!=" in rendered:
            left, right = rendered.split("!=", 1)
            return left.strip() != right.strip()
        lowered = rendered.lower()
        return lowered not in {"", "0", "false", "none", "null", "no"}

    def _emit_step_audit(self, workflow: WorkflowDefinition, result: WorkflowStepResult) -> None:
        if self._audit_handler is None:
            return
        self._audit_handler(
            {
                "workflow_name": workflow.name,
                "step_id": result.step_id,
                "step_type": result.step_type,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "error": result.error,
                "output": result.output,
                "source_path": workflow.source_path,
            }
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
