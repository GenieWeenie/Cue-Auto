"""Tests for ApprovalGate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cue_agent.security.approval_gate import ApprovalGate
from cue_agent.security.risk_classifier import RiskClassifier


def _macro_with_steps():
    return SimpleNamespace(
        steps=[
            SimpleNamespace(
                step_id="1", tool_name="hashed_shell", arguments={"command": "rm -rf /tmp/demo"}, approval=None
            ),
            SimpleNamespace(step_id="2", tool_name="read_file", arguments={"path": "README.md"}, approval=None),
        ]
    )


def test_inject_approvals_marks_high_risk_steps():
    classifier = RiskClassifier(["run_shell"])
    events: list[dict] = []
    gate = ApprovalGate(
        classifier=classifier,
        tool_name_lookup={"hashed_shell": "run_shell"},
        risk_event_handler=lambda event: events.append(event),
    )

    macro = _macro_with_steps()
    updated = gate.inject_approvals(macro)

    assert updated.steps[0].approval is not None
    assert updated.steps[0].approval.required is True
    assert updated.steps[1].approval is None
    assert events[0]["event"] == "high_risk_action"


@pytest.mark.asyncio
async def test_request_approval_denies_without_gateway():
    gate = ApprovalGate(classifier=RiskClassifier(["run_shell"]), approval_gateway=None)
    allowed = await gate.request_approval("do risky thing", "step-1")
    assert allowed is False


class _FakeGateway:
    async def request_approval(self, action_description: str, step_id: str) -> bool:
        return action_description == "safe enough" and step_id == "step-2"


@pytest.mark.asyncio
async def test_request_approval_uses_gateway():
    gate = ApprovalGate(classifier=RiskClassifier(["run_shell"]), approval_gateway=_FakeGateway())
    allowed = await gate.request_approval("safe enough", "step-2")
    assert allowed is True


@pytest.mark.asyncio
async def test_request_approval_denied_in_sandbox_dry_run():
    gate = ApprovalGate(
        classifier=RiskClassifier(["run_shell"], sandbox_dry_run=True),
        approval_gateway=_FakeGateway(),
    )
    allowed = await gate.request_approval("safe enough", "step-2")
    assert allowed is False
