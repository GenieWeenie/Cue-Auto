"""Tests for workflow builder loader/engine/manager behavior."""

from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace

import pytest

from cue_agent.workflows.engine import WorkflowEngine
from cue_agent.workflows.loader import WorkflowDefinition, WorkflowLoader, WorkflowTrigger
from cue_agent.workflows.manager import WorkflowManager


class _FakeBrain:
    def chat(self, user_input: str, extra_context: str = "") -> str:
        return f"chat:{extra_context}|{user_input}"


class _FakeActions:
    def __init__(self):
        self._attempts = 0

        async def _echo(message: str) -> dict[str, object]:
            self._attempts += 1
            if self._attempts == 1:
                raise RuntimeError("transient")
            return {"ok": True, "message": message}

        self.eap_registry = SimpleNamespace(_tools={"echo": _echo})


class _FakeRiskClassifier:
    def assess(self, tool_name: str, arguments=None, execution_context=None):  # noqa: ANN001, ARG002
        return SimpleNamespace(requires_approval=False, reason="ok", level="low")


class _FakeApprovalGateway:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.approved = True

    async def request_approval(self, action_description: str, step_id: str, timeout: int = 300) -> bool:  # noqa: ARG002
        self.requests.append((action_description, step_id))
        return self.approved


def _write_workflow_file(path: Path) -> None:
    path.write_text(
        """
name: demo-workflow
description: End-to-end workflow test
trigger:
  manual: true
  schedules:
    - "0 9 * * *"
  events:
    - "loop.task_completion"
    - "file.*"
steps:
  - id: draft
    type: llm
    prompt: "Draft for {{input.text}}"
  - id: echo_call
    type: tool
    tool: echo
    retry_attempts: 2
    arguments:
      message: "{{draft.output.text}}"
  - id: check
    type: condition
    expression: "{{echo_call.output.result.ok}} == True"
    if_steps:
      - id: approved
        type: approval
        prompt: "Approve message {{echo_call.output.result.message}}?"
    else_steps:
      - id: failed_notice
        type: notification
        title: failure
        body: "unexpected"
  - id: fanout
    type: parallel
    branches:
      - id: one
        steps:
          - id: p1
            type: llm
            prompt: "Branch one {{draft.output.text}}"
      - id: two
        steps:
          - id: p2
            type: llm
            prompt: "Branch two {{echo_call.output.result.message}}"
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_workflow_loader_engine_and_manager(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    templates_dir = workflows_dir / "templates"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "daily-standup-report.yaml").write_text(
        "name: x\nsteps:\n  - id: s1\n    type: llm\n", encoding="utf-8"
    )
    workflow_file = workflows_dir / "demo.yaml"
    _write_workflow_file(workflow_file)

    notifications: list[dict[str, object]] = []
    audits: list[dict[str, object]] = []
    loader = WorkflowLoader(str(workflows_dir))
    engine = WorkflowEngine(
        brain=_FakeBrain(),  # type: ignore[arg-type]
        actions=_FakeActions(),  # type: ignore[arg-type]
        risk_classifier=_FakeRiskClassifier(),  # type: ignore[arg-type]
        approval_gateway=_FakeApprovalGateway(),  # type: ignore[arg-type]
        notification_handler=lambda event: notifications.append(event),
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        retry_jitter_seconds=0.0,
        audit_handler=lambda event: audits.append(event),
    )
    manager = WorkflowManager(loader, engine)

    assert manager.workflow_names == ["demo-workflow"]
    assert manager.list_templates() == ["daily-standup-report"]
    assert [row.workflow_name for row in manager.scheduled_triggers()] == ["demo-workflow"]
    assert manager.event_workflows("loop.task_completion") == ["demo-workflow"]
    assert manager.event_workflows("file.change") == ["demo-workflow"]

    run = await manager.run_workflow("demo-workflow", trigger="manual", input_text="topic-a", actor_user_id="u1")
    assert run.status == "success"
    assert len(run.step_results) == 4
    assert all(row.duration_ms >= 0 for row in run.step_results)
    assert len(audits) >= 4

    tasks = manager.fire_event("loop.task_completion", payload={"category": "task_completion"})
    assert len(tasks) == 1
    event_run = await tasks[0]
    assert event_run.status == "success"


@pytest.mark.asyncio
async def test_workflow_notification_step(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "notify.yaml").write_text(
        """
name: notify-only
trigger:
  manual: true
steps:
  - id: n1
    type: notification
    title: "Hello"
    body: "Workflow {{input.text}}"
""".strip(),
        encoding="utf-8",
    )
    sent: list[dict[str, object]] = []
    loader = WorkflowLoader(str(workflows_dir))
    manager = WorkflowManager(
        loader,
        WorkflowEngine(
            brain=_FakeBrain(),  # type: ignore[arg-type]
            actions=_FakeActions(),  # type: ignore[arg-type]
            risk_classifier=_FakeRiskClassifier(),  # type: ignore[arg-type]
            approval_gateway=None,
            notification_handler=lambda event: sent.append(event),
        ),
    )
    run = await manager.run_workflow("notify-only", trigger="manual", input_text="ping")
    assert run.status == "success"
    assert sent[0]["title"] == "Hello"
    assert sent[0]["body"] == "Workflow ping"


@pytest.mark.asyncio
async def test_workflow_engine_failure_and_branch_paths():
    class _Classifier:
        def assess(self, tool_name: str, arguments=None, execution_context=None):  # noqa: ANN001, ARG002
            if tool_name == "danger":
                return SimpleNamespace(requires_approval=True, reason="dangerous", level="high")
            return SimpleNamespace(requires_approval=False, reason="ok", level="low")

    class _Actions:
        def __init__(self):
            def _danger(text: str) -> str:
                return text

            self.eap_registry = SimpleNamespace(_tools={"danger": _danger})

    approval = _FakeApprovalGateway()
    approval.approved = False
    notifications: list[dict[str, object]] = []
    engine = WorkflowEngine(
        brain=_FakeBrain(),  # type: ignore[arg-type]
        actions=_Actions(),  # type: ignore[arg-type]
        risk_classifier=_Classifier(),  # type: ignore[arg-type]
        approval_gateway=approval,  # type: ignore[arg-type]
        notification_handler=lambda event: notifications.append(event),
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        retry_jitter_seconds=0.0,
    )
    workflow = WorkflowDefinition(
        name="branchy",
        description="",
        source_path="branchy.yaml",
        trigger=WorkflowTrigger(manual=True, schedules=(), events=()),
        steps=(
            {
                "id": "skip_me",
                "type": "llm",
                "when": "{{input.text}} == never",
                "prompt": "ignored",
            },
            {
                "id": "danger_step",
                "type": "tool",
                "tool": "danger",
                "arguments": {"text": "x"},
                "continue_on_error": True,
            },
            {
                "id": "if_else",
                "type": "condition",
                "expression": "{{danger_step.status}} == success",
                "if_steps": [],
                "else_steps": [{"id": "notify_fail", "type": "notification", "title": "Fail", "body": "denied"}],
            },
            {
                "id": "parallel_bad",
                "type": "parallel",
                "branches": [
                    {"id": "good", "steps": [{"id": "g1", "type": "llm", "prompt": "ok"}]},
                    {"id": "bad", "steps": [{"id": "b1", "type": "unsupported"}]},
                ],
                "continue_on_error": True,
            },
            {"id": "bad_type", "type": "unsupported"},
        ),
    )

    run = await engine.run(workflow, trigger="manual", input_text="topic")
    assert run.status == "failed"
    assert run.step_results[0].output["skipped"] is True
    assert run.step_results[1].status == "failed"
    assert notifications and notifications[0]["title"] == "Fail"
    assert approval.requests


def test_workflow_loader_validation_errors(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    bad_file = workflows_dir / "bad.yaml"
    bad_file.write_text("description: missing name\nsteps: []\n", encoding="utf-8")
    loader = WorkflowLoader(str(workflows_dir))
    with pytest.raises(ValueError):
        _ = loader.load_file(bad_file)


def test_workflow_loader_template_variables(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = workflows_dir / "templated.yaml"
    workflow_file.write_text(
        """
name: templated-workflow
description: "Hello {{ NAME }}"
trigger:
  manual: true
steps:
  - id: greet
    type: llm
    prompt: "Say hello to {{ NAME }}"
""".strip(),
        encoding="utf-8",
    )
    loader = WorkflowLoader(str(workflows_dir))
    loaded = loader.load_file(workflow_file, variables={"NAME": "World"})
    assert loaded.description == "Hello World"
    assert loaded.steps[0]["prompt"] == "Say hello to World"


@pytest.mark.asyncio
async def test_workflow_manager_refresh_and_unknown_workflow(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = workflows_dir / "demo.yaml"
    _write_workflow_file(workflow_file)
    manager = WorkflowManager(
        WorkflowLoader(str(workflows_dir)),
        WorkflowEngine(
            brain=_FakeBrain(),  # type: ignore[arg-type]
            actions=_FakeActions(),  # type: ignore[arg-type]
            risk_classifier=_FakeRiskClassifier(),  # type: ignore[arg-type]
            approval_gateway=_FakeApprovalGateway(),  # type: ignore[arg-type]
            notification_handler=None,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            retry_jitter_seconds=0.0,
        ),
    )
    with pytest.raises(ValueError):
        _ = await manager.run_workflow("missing", trigger="manual")
    workflow_file.write_text(
        workflow_file.read_text(encoding="utf-8").replace("demo-workflow", "demo-workflow-2"),
        encoding="utf-8",
    )
    assert manager.refresh_if_needed() is True
