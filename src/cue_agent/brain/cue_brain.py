"""High-level brain facade wrapping EAP's AgentClient with SOUL injection."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, cast

from eap.agent.agent_client import AgentClient
from eap.protocol.models import BatchedMacroRequest

from cue_agent.brain.llm_router import LLMRouter
from cue_agent.brain.soul_loader import SoulLoader
from cue_agent.config import CueConfig

logger = logging.getLogger(__name__)


class CueBrain:
    def __init__(self, config: CueConfig, soul_loader: SoulLoader, router: LLMRouter):
        self.soul = soul_loader
        self.router = router
        self.client = AgentClient(
            base_url=config.openai_base_url,
            model_name=config.openai_model,
            system_prompt=soul_loader.inject(""),
            temperature=config.llm_temperature,
            timeout_seconds=config.llm_timeout_seconds,
            provider=router,
        )

    def chat(self, user_input: str, extra_context: str = "") -> str:
        """Simple text chat with SOUL identity injected."""
        prompt = user_input
        if extra_context:
            prompt = f"{extra_context}\n\n{user_input}"
        return cast(str, self.client.chat(prompt))

    def plan(
        self,
        task_description: str,
        hashed_manifest: Dict[str, Any],
        memory_context: str = "",
    ) -> BatchedMacroRequest:
        """Generate an EAP macro (DAG execution plan) for a task."""
        return self.client.generate_macro(
            user_input=task_description,
            hashed_manifest=hashed_manifest,
            memory_context=memory_context,
        )

    def stream_chat(
        self,
        user_input: str,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Stream a chat response token by token."""
        return cast(str, self.client.stream_chat(user_input, on_token=on_token))
