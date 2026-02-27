"""Cascading LLM provider: OpenAI -> Claude -> OpenRouter -> LM Studio."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Iterable

from agent.providers.base import (
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    ProviderMessage,
)
from agent.providers.factory import create_provider

from cue_agent.config import CueConfig

logger = logging.getLogger(__name__)


class LLMRouter(LLMProvider):
    """Implements EAP's LLMProvider with cascading fallback across multiple providers."""

    def __init__(self, config: CueConfig):
        self._providers: list[tuple[str, LLMProvider, str]] = []
        timeout = config.llm_timeout_seconds

        # Primary: OpenAI
        if config.has_openai:
            self._providers.append((
                "openai",
                create_provider(
                    provider_name="openai",
                    base_url=config.openai_base_url,
                    api_key=config.openai_api_key,
                    timeout_seconds=timeout,
                ),
                config.openai_model,
            ))

        # Fallback 1: Anthropic Claude
        if config.has_anthropic:
            self._providers.append((
                "anthropic",
                create_provider(
                    provider_name="anthropic",
                    base_url="https://api.anthropic.com",
                    api_key=config.anthropic_api_key,
                    timeout_seconds=timeout,
                ),
                config.anthropic_model,
            ))

        # Fallback 2: OpenRouter (OpenAI-compatible)
        if config.has_openrouter:
            self._providers.append((
                "openrouter",
                create_provider(
                    provider_name="openai",
                    base_url=config.openrouter_base_url,
                    api_key=config.openrouter_api_key,
                    timeout_seconds=timeout,
                ),
                config.openrouter_model,
            ))

        # Fallback 3: LM Studio (local, always available)
        self._providers.append((
            "lmstudio",
            create_provider(
                provider_name="local",
                base_url=config.lmstudio_base_url,
                api_key="not-needed",
                timeout_seconds=timeout,
            ),
            config.lmstudio_model,
        ))

        logger.info(
            "LLMRouter initialized with %d providers: %s",
            len(self._providers),
            [name for name, _, _ in self._providers],
        )

    def _adapt_request(self, request: CompletionRequest, model: str) -> CompletionRequest:
        """Swap the model name in a request for a specific provider."""
        return CompletionRequest(
            model=model,
            messages=request.messages,
            temperature=request.temperature,
            tools=request.tools,
            metadata=request.metadata,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        last_error: Exception | None = None
        for name, provider, model in self._providers:
            try:
                adapted = self._adapt_request(request, model)
                response = provider.complete(adapted)
                logger.info("complete() succeeded via %s", name)
                return response
            except Exception as e:
                logger.warning("Provider %s failed: %s", name, e)
                last_error = e
        raise last_error  # type: ignore[misc]

    def complete_with_tools(self, request: CompletionRequest) -> CompletionResponse:
        last_error: Exception | None = None
        for name, provider, model in self._providers:
            try:
                adapted = self._adapt_request(request, model)
                response = provider.complete_with_tools(adapted)
                logger.info("complete_with_tools() succeeded via %s", name)
                return response
            except Exception as e:
                logger.warning("Provider %s failed: %s", name, e)
                last_error = e
        raise last_error  # type: ignore[misc]

    def stream(self, request: CompletionRequest) -> Iterable[str]:
        last_error: Exception | None = None
        for name, provider, model in self._providers:
            try:
                adapted = self._adapt_request(request, model)
                logger.info("stream() attempting via %s", name)
                return provider.stream(adapted)
            except Exception as e:
                logger.warning("Provider %s failed: %s", name, e)
                last_error = e
        raise last_error  # type: ignore[misc]

    def health_check(self) -> dict[str, bool]:
        """Ping each provider with a trivial request and return reachability."""
        results: dict[str, bool] = {}
        for name, provider, model in self._providers:
            try:
                req = CompletionRequest(
                    model=model,
                    messages=[ProviderMessage(role="user", content="ping")],
                    temperature=0.0,
                )
                provider.complete(req)
                results[name] = True
            except Exception:
                results[name] = False
        return results
