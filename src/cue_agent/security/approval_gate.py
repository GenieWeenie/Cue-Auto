"""Bridges EAP HITL checkpoints to Telegram approval flow."""

from __future__ import annotations

import logging
from typing import Any, Callable

from eap.protocol.models import BatchedMacroRequest, StepApprovalCheckpoint

from cue_agent.comms.approval_gateway import ApprovalGateway
from cue_agent.security.risk_classifier import RiskClassifier

logger = logging.getLogger(__name__)


class ApprovalGate:
    """Scans macro steps for high-risk tools and injects approval checkpoints."""

    def __init__(
        self,
        classifier: RiskClassifier,
        approval_gateway: ApprovalGateway | None = None,
        tool_name_lookup: dict[str, str] | None = None,
        risk_event_handler: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._classifier = classifier
        self._gateway = approval_gateway
        self._tool_name_lookup = tool_name_lookup or {}
        self._risk_event_handler = risk_event_handler

    def inject_approvals(self, macro: BatchedMacroRequest) -> BatchedMacroRequest:
        """Add StepApprovalCheckpoint to high-risk steps in the macro."""
        for step in macro.steps:
            # Resolve hashed tool name back to original
            original = self._tool_name_lookup.get(step.tool_name, step.tool_name)
            args = step.arguments if isinstance(step.arguments, dict) else {}
            decision = self._classifier.assess(
                original,
                arguments=args,
                execution_context={"source": "macro_execution"},
            )
            if decision.requires_approval:
                step.approval = StepApprovalCheckpoint(
                    required=True,
                    prompt=(
                        f"{decision.level.upper()}-risk action: {original}; "
                        f"reason={decision.reason}; args={step.arguments}"
                    ),
                )
                logger.info(
                    "Injected approval checkpoint for step %s (%s)",
                    step.step_id,
                    original,
                    extra={
                        "event": "approval_injected",
                        "tool_name": original,
                        "risk_level": decision.level,
                        "dry_run": decision.sandbox_dry_run,
                    },
                )
                if self._risk_event_handler is not None:
                    self._risk_event_handler(
                        {
                            "event": "high_risk_action",
                            "tool_name": original,
                            "risk_level": decision.level,
                            "reason": decision.reason,
                            "step_id": step.step_id,
                        }
                    )
        return macro

    async def request_approval(self, action_description: str, step_id: str) -> bool:
        """Send an approval request via the approval gateway."""
        if self._classifier.sandbox_dry_run:
            logger.warning(
                "Sandbox dry-run active — auto-denying step %s",
                step_id,
                extra={"event": "approval_dry_run_deny", "step_id": step_id},
            )
            return False
        if self._gateway is None:
            logger.warning("No approval gateway configured — auto-denying step %s", step_id)
            return False
        return await self._gateway.request_approval(action_description, step_id)
