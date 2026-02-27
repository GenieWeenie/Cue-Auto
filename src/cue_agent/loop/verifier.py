"""Post-execution verification — asks the LLM to evaluate success."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cue_agent.brain.cue_brain import CueBrain

logger = logging.getLogger(__name__)

VERIFY_PROMPT = """You are evaluating the result of an automated task execution.

Task: {task}
Execution result: {result}

Answer with:
1. SUCCESS or FAILURE (first word)
2. A one-sentence summary of what happened

Example: SUCCESS — File was read and contents returned as expected."""


@dataclass
class VerificationResult:
    success: bool
    summary: str


class Verifier:
    def __init__(self, brain: CueBrain):
        self._brain = brain

    def verify(self, task: str, result: str) -> VerificationResult:
        """Ask the LLM if the task execution was successful."""
        response = self._brain.chat(VERIFY_PROMPT.format(task=task, result=result))
        response = response.strip()

        success = response.upper().startswith("SUCCESS")
        logger.info("Verification: %s — %s", "SUCCESS" if success else "FAILURE", response[:100])
        return VerificationResult(success=success, summary=response)
