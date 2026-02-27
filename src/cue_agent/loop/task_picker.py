"""LLM-driven task selection from current state context."""

from __future__ import annotations

import logging

from cue_agent.brain.cue_brain import CueBrain

logger = logging.getLogger(__name__)

PICK_PROMPT = """You are an autonomous task scheduler. Given the current state below, decide what to do next.

Rules:
- Pick exactly ONE task — the highest-priority actionable item
- If there is nothing to do, respond with exactly: NOTHING
- Be specific and concise in your task description
- Do not explain your reasoning, just state the task

Current state:
{context}

What is the single most important task to do next?"""


class TaskPicker:
    def __init__(self, brain: CueBrain):
        self._brain = brain

    def pick(self, context: str) -> str | None:
        """Ask the LLM to select the next task. Returns None if idle."""
        response = self._brain.chat(PICK_PROMPT.format(context=context))
        response = response.strip()

        if "NOTHING" in response.upper():
            logger.info("TaskPicker: no actionable tasks")
            return None

        logger.info("TaskPicker selected: %s", response[:100])
        return response
