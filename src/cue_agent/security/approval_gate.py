"""Bridges EAP HITL checkpoints to Telegram approval flow."""

from __future__ import annotations

import logging

from protocol.models import BatchedMacroRequest, StepApprovalCheckpoint

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
    ):
        self._classifier = classifier
        self._gateway = approval_gateway
        self._tool_name_lookup = tool_name_lookup or {}

    def inject_approvals(self, macro: BatchedMacroRequest) -> BatchedMacroRequest:
        """Add StepApprovalCheckpoint to high-risk steps in the macro."""
        for step in macro.steps:
            # Resolve hashed tool name back to original
            original = self._tool_name_lookup.get(step.tool_name, step.tool_name)
            if self._classifier.is_high_risk(original):
                step.approval = StepApprovalCheckpoint(
                    required=True,
                    prompt=f"High-risk action: {original} with args {step.arguments}",
                )
                logger.info("Injected approval checkpoint for step %s (%s)", step.step_id, original)
        return macro

    async def request_approval(self, action_description: str, step_id: str) -> bool:
        """Send an approval request via the approval gateway."""
        if self._gateway is None:
            logger.warning("No approval gateway configured — auto-denying step %s", step_id)
            return False
        return await self._gateway.request_approval(action_description, step_id)
