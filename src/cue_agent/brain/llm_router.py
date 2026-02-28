"""Cascading LLM provider: OpenAI -> Claude -> OpenRouter -> LM Studio."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, cast

from agent.providers.base import (
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    ProviderMessage,
)
from agent.providers.factory import create_provider

from cue_agent.config import CueConfig
from cue_agent.retry_utils import backoff_delay_seconds

logger = logging.getLogger(__name__)


class LLMAllProvidersDownError(RuntimeError):
    """Raised when every provider fails or is open-circuit."""

    def __init__(self, provider_status: dict[str, str], last_error: Exception | None = None):
        self.provider_status = provider_status
        self.last_error = last_error
        message = f"All LLM providers unavailable: {provider_status}"
        if last_error is not None:
            message = f"{message}; last_error={last_error}"
        super().__init__(message)


@dataclass
class ProviderRuntimeState:
    consecutive_failures: int = 0
    open_until_ts: float = 0.0
    last_error: str = ""
    last_success_ts: float = 0.0


class LLMRouter:
    """Implements EAP's LLMProvider with cascading fallback across multiple providers."""

    def __init__(self, config: CueConfig):
        self._providers: list[tuple[str, LLMProvider, str]] = []
        self._provider_state: dict[str, ProviderRuntimeState] = {}
        self._retry_llm_attempts = max(1, config.retry_llm_attempts)
        self._retry_base_delay = config.retry_base_delay_seconds
        self._retry_max_delay = config.retry_max_delay_seconds
        self._retry_jitter = config.retry_jitter_seconds
        self._circuit_breaker_failures = max(1, config.circuit_breaker_failures)
        self._circuit_breaker_cooldown_seconds = max(1, config.circuit_breaker_cooldown_seconds)
        timeout = config.llm_timeout_seconds

        # Primary: OpenAI
        if config.has_openai:
            self._providers.append(
                (
                    "openai",
                    create_provider(
                        provider_name="openai",
                        base_url=config.openai_base_url,
                        api_key=config.openai_api_key,
                        timeout_seconds=timeout,
                    ),
                    config.openai_model,
                )
            )

        # Fallback 1: Anthropic Claude
        if config.has_anthropic:
            self._providers.append(
                (
                    "anthropic",
                    create_provider(
                        provider_name="anthropic",
                        base_url="https://api.anthropic.com",
                        api_key=config.anthropic_api_key,
                        timeout_seconds=timeout,
                    ),
                    config.anthropic_model,
                )
            )

        # Fallback 2: OpenRouter (OpenAI-compatible)
        if config.has_openrouter:
            self._providers.append(
                (
                    "openrouter",
                    create_provider(
                        provider_name="openai",
                        base_url=config.openrouter_base_url,
                        api_key=config.openrouter_api_key,
                        timeout_seconds=timeout,
                    ),
                    config.openrouter_model,
                )
            )

        # Fallback 3: LM Studio (local, always available)
        self._providers.append(
            (
                "lmstudio",
                create_provider(
                    provider_name="local",
                    base_url=config.lmstudio_base_url,
                    api_key="not-needed",
                    timeout_seconds=timeout,
                ),
                config.lmstudio_model,
            )
        )

        logger.info(
            "LLMRouter initialized with %d providers: %s",
            len(self._providers),
            [name for name, _, _ in self._providers],
        )
        self._provider_state = {name: ProviderRuntimeState() for name, _, _ in self._providers}

    @staticmethod
    def _extract_tokens(response: CompletionResponse) -> tuple[int | None, int | None]:
        raw = response.raw_response or {}
        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        if not isinstance(usage, dict):
            return None, None

        tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens")
        tokens_out = usage.get("completion_tokens") or usage.get("output_tokens")
        try:
            tokens_in = int(tokens_in) if tokens_in is not None else None
        except (TypeError, ValueError):
            tokens_in = None
        try:
            tokens_out = int(tokens_out) if tokens_out is not None else None
        except (TypeError, ValueError):
            tokens_out = None
        return tokens_in, tokens_out

    def _adapt_request(self, request: CompletionRequest, model: str) -> CompletionRequest:
        """Swap the model name in a request for a specific provider."""
        return CompletionRequest(
            model=model,
            messages=request.messages,
            temperature=request.temperature,
            tools=request.tools,
            metadata=request.metadata,
        )

    def _is_circuit_open(self, provider_name: str) -> bool:
        state = self._provider_state[provider_name]
        return time.time() < state.open_until_ts

    def _record_provider_failure(self, provider_name: str, error: Exception | str) -> None:
        state = self._provider_state[provider_name]
        state.consecutive_failures += 1
        state.last_error = str(error)
        if state.consecutive_failures >= self._circuit_breaker_failures:
            state.open_until_ts = time.time() + self._circuit_breaker_cooldown_seconds
            logger.warning(
                "Provider circuit opened",
                extra={
                    "event": "llm_circuit_open",
                    "provider": provider_name,
                    "cooldown_seconds": self._circuit_breaker_cooldown_seconds,
                    "consecutive_failures": state.consecutive_failures,
                    "error": state.last_error,
                },
            )

    def _record_provider_success(self, provider_name: str) -> None:
        state = self._provider_state[provider_name]
        state.consecutive_failures = 0
        state.open_until_ts = 0.0
        state.last_error = ""
        state.last_success_ts = time.time()

    def _provider_availability_summary(self) -> dict[str, str]:
        summary: dict[str, str] = {}
        for name, _, _ in self._providers:
            state = self._provider_state[name]
            if self._is_circuit_open(name):
                remaining = int(max(0, state.open_until_ts - time.time()))
                summary[name] = f"circuit_open:{remaining}s"
            elif state.last_error:
                summary[name] = f"error:{state.last_error}"
            else:
                summary[name] = "unknown"
        return summary

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        last_error: Exception | None = None
        for name, provider, model in self._providers:
            if self._is_circuit_open(name):
                logger.warning(
                    "Provider skipped due to open circuit",
                    extra={"event": "llm_provider_skipped", "provider": name, "operation": "complete"},
                )
                continue

            for attempt in range(1, self._retry_llm_attempts + 1):
                start = time.monotonic()
                try:
                    adapted = self._adapt_request(request, model)
                    response = provider.complete(adapted)
                    latency_ms = int((time.monotonic() - start) * 1000)
                    tokens_in, tokens_out = self._extract_tokens(response)
                    self._record_provider_success(name)
                    logger.info(
                        "LLM completion succeeded",
                        extra={
                            "event": "llm_call",
                            "provider": name,
                            "model": model,
                            "latency_ms": latency_ms,
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "operation": "complete",
                            "attempt": attempt,
                        },
                    )
                    return response
                except Exception as e:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    last_error = e
                    logger.warning(
                        "LLM completion failed",
                        extra={
                            "event": "llm_call_failed",
                            "provider": name,
                            "model": model,
                            "latency_ms": latency_ms,
                            "operation": "complete",
                            "attempt": attempt,
                            "error": str(e),
                        },
                    )
                    if attempt < self._retry_llm_attempts:
                        delay = backoff_delay_seconds(
                            attempt,
                            base_delay=self._retry_base_delay,
                            max_delay=self._retry_max_delay,
                            jitter=self._retry_jitter,
                        )
                        time.sleep(delay)
                        continue
                    self._record_provider_failure(name, e)
                    break

        raise LLMAllProvidersDownError(self._provider_availability_summary(), last_error=last_error)

    def complete_with_tools(self, request: CompletionRequest) -> CompletionResponse:
        last_error: Exception | None = None
        for name, provider, model in self._providers:
            if self._is_circuit_open(name):
                logger.warning(
                    "Provider skipped due to open circuit",
                    extra={"event": "llm_provider_skipped", "provider": name, "operation": "complete_with_tools"},
                )
                continue

            for attempt in range(1, self._retry_llm_attempts + 1):
                start = time.monotonic()
                try:
                    adapted = self._adapt_request(request, model)
                    response = provider.complete_with_tools(adapted)
                    latency_ms = int((time.monotonic() - start) * 1000)
                    tokens_in, tokens_out = self._extract_tokens(response)
                    self._record_provider_success(name)
                    logger.info(
                        "LLM tool-completion succeeded",
                        extra={
                            "event": "llm_call",
                            "provider": name,
                            "model": model,
                            "latency_ms": latency_ms,
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "operation": "complete_with_tools",
                            "attempt": attempt,
                        },
                    )
                    return response
                except Exception as e:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    last_error = e
                    logger.warning(
                        "LLM tool-completion failed",
                        extra={
                            "event": "llm_call_failed",
                            "provider": name,
                            "model": model,
                            "latency_ms": latency_ms,
                            "operation": "complete_with_tools",
                            "attempt": attempt,
                            "error": str(e),
                        },
                    )
                    if attempt < self._retry_llm_attempts:
                        delay = backoff_delay_seconds(
                            attempt,
                            base_delay=self._retry_base_delay,
                            max_delay=self._retry_max_delay,
                            jitter=self._retry_jitter,
                        )
                        time.sleep(delay)
                        continue
                    self._record_provider_failure(name, e)
                    break

        raise LLMAllProvidersDownError(self._provider_availability_summary(), last_error=last_error)

    def stream(self, request: CompletionRequest) -> Iterable[str]:
        last_error: Exception | None = None
        for name, provider, model in self._providers:
            if self._is_circuit_open(name):
                logger.warning(
                    "Provider skipped due to open circuit",
                    extra={"event": "llm_provider_skipped", "provider": name, "operation": "stream"},
                )
                continue

            start = time.monotonic()
            try:
                adapted = self._adapt_request(request, model)
                logger.info(
                    "LLM stream started",
                    extra={
                        "event": "llm_call",
                        "provider": name,
                        "model": model,
                        "operation": "stream",
                    },
                )
                self._record_provider_success(name)
                return cast(Iterable[str], provider.stream(adapted))
            except Exception as e:
                latency_ms = int((time.monotonic() - start) * 1000)
                logger.warning(
                    "LLM stream failed",
                    extra={
                        "event": "llm_call_failed",
                        "provider": name,
                        "model": model,
                        "latency_ms": latency_ms,
                        "operation": "stream",
                        "error": str(e),
                    },
                )
                last_error = e
                self._record_provider_failure(name, e)
        raise LLMAllProvidersDownError(self._provider_availability_summary(), last_error=last_error)

    def health_check(self) -> dict[str, bool]:
        """Ping each provider with a trivial request and return reachability."""
        results: dict[str, bool] = {}
        for name, provider, model in self._providers:
            if self._is_circuit_open(name):
                results[name] = False
                continue
            try:
                req = CompletionRequest(
                    model=model,
                    messages=[ProviderMessage(role="user", content="ping")],
                    temperature=0.0,
                )
                provider.complete(req)
                results[name] = True
                self._record_provider_success(name)
            except Exception:
                results[name] = False
                self._record_provider_failure(name, "health_check_failed")
        return results

    def health_status(self) -> dict[str, str]:
        """Return best-known provider status without making network calls."""
        results: dict[str, str] = {}
        for name, _, _ in self._providers:
            state = self._provider_state[name]
            if self._is_circuit_open(name):
                results[name] = "down"
            elif state.last_error:
                results[name] = "down"
            elif state.last_success_ts > 0:
                results[name] = "up"
            else:
                results[name] = "unknown"
        return results
