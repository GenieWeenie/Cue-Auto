"""Integration-style tests for approval flow."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cue_agent.comms.approval_gateway import ApprovalGateway
from cue_agent.security.approval_gate import ApprovalGate
from cue_agent.security.risk_classifier import RiskClassifier


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


@pytest.mark.asyncio
async def test_approval_gateway_request_then_callback():
    bot = _FakeBot()
    gateway = ApprovalGateway(bot=bot, admin_chat_id=999)

    request_task = asyncio.create_task(gateway.request_approval("Delete file?", "step-1", timeout=1))
    await asyncio.sleep(0)

    approval_id = next(iter(gateway._pending.keys()))
    await gateway.handle_callback(approval_id, approved=True)
    decision = await request_task

    assert decision is True
    assert gateway._pending == {}
    assert len(bot.sent) == 1
    assert "APPROVAL REQUIRED" in bot.sent[0]["text"]
    keyboard = bot.sent[0]["reply_markup"]
    approve_data = keyboard.inline_keyboard[0][0].callback_data
    deny_data = keyboard.inline_keyboard[0][1].callback_data
    assert approve_data == f"approve:{approval_id}"
    assert deny_data == f"deny:{approval_id}"


@pytest.mark.asyncio
async def test_approval_gateway_timeout_defaults_deny():
    gateway = ApprovalGateway(bot=_FakeBot(), admin_chat_id=999)
    decision = await gateway.request_approval("Do risky action", "step-2", timeout=0.01)
    assert decision is False
    assert gateway._pending == {}


@pytest.mark.asyncio
async def test_end_to_end_approval_gate_with_gateway():
    bot = _FakeBot()
    gateway = ApprovalGateway(bot=bot, admin_chat_id=999)
    gate = ApprovalGate(
        classifier=RiskClassifier(["run_shell"]),
        approval_gateway=gateway,
        tool_name_lookup={"hash1": "run_shell"},
    )

    macro = SimpleNamespace(
        steps=[SimpleNamespace(step_id="s1", tool_name="hash1", arguments={"command": "rm -rf /"}, approval=None)]
    )
    macro = gate.inject_approvals(macro)
    assert macro.steps[0].approval.required is True

    request_task = asyncio.create_task(gate.request_approval("Run dangerous command", "s1"))
    await asyncio.sleep(0)
    approval_id = next(iter(gateway._pending.keys()))
    await gateway.handle_callback(approval_id, approved=False)
    decision = await request_task

    assert decision is False
