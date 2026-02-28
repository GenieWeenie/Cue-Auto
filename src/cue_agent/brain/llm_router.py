"""Cascading LLM provider: OpenAI -> Claude -> OpenRouter -> LM Studio."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, cast

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
_SIMPLE_COMPLEXITY_MAX_CHARS = 400
_SIMPLE_COMPLEXITY_MAX_MESSAGES = 2
_SIMPLE_COMPLEXITY_MAX_TOOLS = 2
_SIMPLE_PROVIDER_ORDER = ("lmstudio", "openrouter", "openai", "anthropic")
_COMPLEX_PROVIDER_ORDER = ("openai", "anthropic", "openrouter", "lmstudio")
_COMPLEXITY_KEYWORDS = (
    "analyze",
    "architecture",
    "compare",
    "debug",
    "design",
    "multi-step",
    "reason",
    "strategy",
)


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
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_total_ms: int = 0
    estimated_cost_usd: float = 0.0
    last_model: str = ""
    last_used_ts: float = 0.0


class LLMRouter:
    """Implements EAP's LLMProvider with cascading fallback across multiple providers."""

    def __init__(
        self,
        config: CueConfig,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._providers: dict[str, tuple[LLMProvider, str]] = {}
        self._provider_state: dict[str, ProviderRuntimeState] = {}
        self._event_handler = event_handler
        self._retry_llm_attempts = max(1, config.retry_llm_attempts)
        self._retry_base_delay = config.retry_base_delay_seconds
        self._retry_max_delay = config.retry_max_delay_seconds
        self._retry_jitter = config.retry_jitter_seconds
        self._circuit_breaker_failures = max(1, config.circuit_breaker_failures)
        self._circuit_breaker_cooldown_seconds = max(1, config.circuit_breaker_cooldown_seconds)
        self._monthly_budget_warning_usd = max(0.0, config.llm_budget_warning_usd)
        self._monthly_budget_hard_stop_usd = max(
            self._monthly_budget_warning_usd,
            config.llm_monthly_budget_usd,
        )
        self._enforce_budget_hard_stop = config.llm_budget_enforce_hard_stop
        self._provider_input_cost_per_1k = {
            "openai": max(0.0, config.llm_cost_openai_input_per_1k),
            "anthropic": max(0.0, config.llm_cost_anthropic_input_per_1k),
            "openrouter": max(0.0, config.llm_cost_openrouter_input_per_1k),
            "lmstudio": max(0.0, config.llm_cost_lmstudio_input_per_1k),
        }
        self._provider_output_cost_per_1k = {
            "openai": max(0.0, config.llm_cost_openai_output_per_1k),
            "anthropic": max(0.0, config.llm_cost_anthropic_output_per_1k),
            "openrouter": max(0.0, config.llm_cost_openrouter_output_per_1k),
            "lmstudio": max(0.0, config.llm_cost_lmstudio_output_per_1k),
        }
        self._usage_month_key = self._current_month_key()
        self._budget_warning_emitted = False
        self._budget_hard_stop_emitted = False
        timeout = config.llm_timeout_seconds

        # Primary: OpenAI
        if config.has_openai:
            self._providers["openai"] = (
                create_provider(
                    provider_name="openai",
                    base_url=config.openai_base_url,
                    api_key=config.openai_api_key,
                    timeout_seconds=timeout,
                ),
                config.openai_model,
            )

        # Fallback 1: Anthropic Claude
        if config.has_anthropic:
            self._providers["anthropic"] = (
                create_provider(
                    provider_name="anthropic",
                    base_url="https://api.anthropic.com",
                    api_key=config.anthropic_api_key,
                    timeout_seconds=timeout,
                ),
                config.anthropic_model,
            )

        # Fallback 2: OpenRouter (OpenAI-compatible)
        if config.has_openrouter:
            self._providers["openrouter"] = (
                create_provider(
                    provider_name="openai",
                    base_url=config.openrouter_base_url,
                    api_key=config.openrouter_api_key,
                    timeout_seconds=timeout,
                ),
                config.openrouter_model,
            )

        # Fallback 3: LM Studio (local, always available)
        self._providers["lmstudio"] = (
            create_provider(
                provider_name="local",
                base_url=config.lmstudio_base_url,
                api_key="not-needed",
                timeout_seconds=timeout,
            ),
            config.lmstudio_model,
        )

        logger.info(
            "LLMRouter initialized with %d providers: %s",
            len(self._providers),
            list(self._providers.keys()),
        )
        self._provider_state = {name: ProviderRuntimeState() for name in self._providers}

    @staticmethod
    def _current_month_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _roll_usage_month_if_needed(self) -> None:
        current = self._current_month_key()
        if current == self._usage_month_key:
            return
        self._usage_month_key = current
        self._budget_warning_emitted = False
        self._budget_hard_stop_emitted = False
        for state in self._provider_state.values():
            state.requests = 0
            state.tokens_in = 0
            state.tokens_out = 0
            state.latency_total_ms = 0
            state.estimated_cost_usd = 0.0

    def _extract_text_for_complexity(self, request: CompletionRequest) -> str:
        pieces: list[str] = []
        for message in request.messages:
            content = message.content
            if isinstance(content, str):
                pieces.append(content)
        return " ".join(pieces)

    def _classify_complexity(self, request: CompletionRequest) -> str:
        text = self._extract_text_for_complexity(request).lower()
        if len(text) > _SIMPLE_COMPLEXITY_MAX_CHARS:
            return "complex"
        if len(request.messages) > _SIMPLE_COMPLEXITY_MAX_MESSAGES:
            return "complex"
        if request.tools and len(request.tools) > _SIMPLE_COMPLEXITY_MAX_TOOLS:
            return "complex"
        if any(keyword in text for keyword in _COMPLEXITY_KEYWORDS):
            return "complex"
        return "simple"

    def _provider_order_for_complexity(self, complexity: str) -> list[str]:
        preferred = _SIMPLE_PROVIDER_ORDER if complexity == "simple" else _COMPLEX_PROVIDER_ORDER
        ordered = [name for name in preferred if name in self._providers]
        for name in self._providers:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def _estimate_cost_usd(
        self,
        provider_name: str,
        tokens_in: int | None,
        tokens_out: int | None,
    ) -> float:
        in_cost = self._provider_input_cost_per_1k.get(provider_name, 0.0)
        out_cost = self._provider_output_cost_per_1k.get(provider_name, 0.0)
        in_tokens = max(0, tokens_in or 0)
        out_tokens = max(0, tokens_out or 0)
        return (in_tokens / 1000.0) * in_cost + (out_tokens / 1000.0) * out_cost

    def _record_usage(
        self,
        provider_name: str,
        model: str,
        *,
        latency_ms: int,
        tokens_in: int | None,
        tokens_out: int | None,
    ) -> None:
        state = self._provider_state[provider_name]
        state.requests += 1
        state.tokens_in += max(0, tokens_in or 0)
        state.tokens_out += max(0, tokens_out or 0)
        state.latency_total_ms += max(0, latency_ms)
        state.estimated_cost_usd += self._estimate_cost_usd(provider_name, tokens_in, tokens_out)
        state.last_model = model
        state.last_used_ts = time.time()

    def _monthly_spend_usd(self) -> float:
        return sum(state.estimated_cost_usd for state in self._provider_state.values())

    def _should_hard_stop_provider(self, provider_name: str) -> bool:
        if not self._enforce_budget_hard_stop:
            return False
        if provider_name == "lmstudio":
            return False
        return self._monthly_spend_usd() >= self._monthly_budget_hard_stop_usd

    def _maybe_log_budget_warning(self) -> None:
        spend = self._monthly_spend_usd()
        if self._budget_warning_emitted:
            return
        if spend < self._monthly_budget_warning_usd:
            return
        self._budget_warning_emitted = True
        logger.warning(
            "LLM monthly budget warning threshold reached",
            extra={
                "event": "llm_budget_warning",
                "month": self._usage_month_key,
                "monthly_spend_usd": round(spend, 6),
                "warning_threshold_usd": self._monthly_budget_warning_usd,
                "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
            },
        )
        if self._event_handler is not None:
            self._event_handler(
                {
                    "event": "llm_budget_warning",
                    "month": self._usage_month_key,
                    "monthly_spend_usd": round(spend, 6),
                    "warning_threshold_usd": self._monthly_budget_warning_usd,
                    "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
                }
            )

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
        for name in self._providers:
            state = self._provider_state[name]
            if self._is_circuit_open(name):
                remaining = int(max(0, state.open_until_ts - time.time()))
                summary[name] = f"circuit_open:{remaining}s"
            elif state.last_error:
                summary[name] = f"error:{state.last_error}"
            else:
                summary[name] = "unknown"
        return summary

    def _invoke_completion(
        self,
        *,
        request: CompletionRequest,
        operation: str,
        call: Callable[[LLMProvider, CompletionRequest], CompletionResponse],
    ) -> CompletionResponse:
        self._roll_usage_month_if_needed()
        complexity = self._classify_complexity(request)
        ordered_names = self._provider_order_for_complexity(complexity)
        logger.info(
            "LLM routing decision",
            extra={
                "event": "llm_routing_decision",
                "operation": operation,
                "complexity": complexity,
                "provider_order": ordered_names,
                "month": self._usage_month_key,
                "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
            },
        )

        skipped_budget = 0
        last_error: Exception | None = None
        for name in ordered_names:
            provider, model = self._providers[name]
            if self._is_circuit_open(name):
                logger.warning(
                    "Provider skipped due to open circuit",
                    extra={"event": "llm_provider_skipped", "provider": name, "operation": operation},
                )
                continue
            if self._should_hard_stop_provider(name):
                skipped_budget += 1
                logger.warning(
                    "Provider skipped due to monthly budget hard stop",
                    extra={
                        "event": "llm_budget_hard_stop",
                        "provider": name,
                        "operation": operation,
                        "month": self._usage_month_key,
                        "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
                        "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
                    },
                )
                if not self._budget_hard_stop_emitted and self._event_handler is not None:
                    self._budget_hard_stop_emitted = True
                    self._event_handler(
                        {
                            "event": "llm_budget_hard_stop",
                            "provider": name,
                            "operation": operation,
                            "month": self._usage_month_key,
                            "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
                            "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
                        }
                    )
                continue

            for attempt in range(1, self._retry_llm_attempts + 1):
                start = time.monotonic()
                try:
                    adapted = self._adapt_request(request, model)
                    response = call(provider, adapted)
                    latency_ms = int((time.monotonic() - start) * 1000)
                    tokens_in, tokens_out = self._extract_tokens(response)
                    self._record_usage(
                        name,
                        model,
                        latency_ms=latency_ms,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                    )
                    self._record_provider_success(name)
                    self._maybe_log_budget_warning()
                    logger.info(
                        "LLM completion succeeded",
                        extra={
                            "event": "llm_call",
                            "provider": name,
                            "model": model,
                            "latency_ms": latency_ms,
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "estimated_cost_usd": round(
                                self._estimate_cost_usd(name, tokens_in, tokens_out),
                                6,
                            ),
                            "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
                            "operation": operation,
                            "attempt": attempt,
                            "complexity": complexity,
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
                            "operation": operation,
                            "attempt": attempt,
                            "error": str(e),
                            "complexity": complexity,
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

        if skipped_budget >= len(ordered_names) and ordered_names:
            raise LLMAllProvidersDownError(
                {"budget": (f"hard_stop:{self._monthly_spend_usd():.6f}>={self._monthly_budget_hard_stop_usd:.6f}")},
                last_error=last_error,
            )
        raise LLMAllProvidersDownError(self._provider_availability_summary(), last_error=last_error)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._invoke_completion(
            request=request,
            operation="complete",
            call=lambda provider, adapted: provider.complete(adapted),
        )

    def complete_with_tools(self, request: CompletionRequest) -> CompletionResponse:
        return self._invoke_completion(
            request=request,
            operation="complete_with_tools",
            call=lambda provider, adapted: provider.complete_with_tools(adapted),
        )

    def stream(self, request: CompletionRequest) -> Iterable[str]:
        self._roll_usage_month_if_needed()
        complexity = self._classify_complexity(request)
        ordered_names = self._provider_order_for_complexity(complexity)
        logger.info(
            "LLM routing decision",
            extra={
                "event": "llm_routing_decision",
                "operation": "stream",
                "complexity": complexity,
                "provider_order": ordered_names,
                "month": self._usage_month_key,
                "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
            },
        )

        last_error: Exception | None = None
        for name in ordered_names:
            provider, model = self._providers[name]
            if self._is_circuit_open(name):
                logger.warning(
                    "Provider skipped due to open circuit",
                    extra={"event": "llm_provider_skipped", "provider": name, "operation": "stream"},
                )
                continue
            if self._should_hard_stop_provider(name):
                logger.warning(
                    "Provider skipped due to monthly budget hard stop",
                    extra={
                        "event": "llm_budget_hard_stop",
                        "provider": name,
                        "operation": "stream",
                        "month": self._usage_month_key,
                        "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
                        "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
                    },
                )
                if not self._budget_hard_stop_emitted and self._event_handler is not None:
                    self._budget_hard_stop_emitted = True
                    self._event_handler(
                        {
                            "event": "llm_budget_hard_stop",
                            "provider": name,
                            "operation": "stream",
                            "month": self._usage_month_key,
                            "monthly_spend_usd": round(self._monthly_spend_usd(), 6),
                            "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
                        }
                    )
                continue

            start = time.monotonic()
            try:
                adapted = self._adapt_request(request, model)
                self._record_usage(name, model, latency_ms=0, tokens_in=None, tokens_out=None)
                logger.info(
                    "LLM stream started",
                    extra={
                        "event": "llm_call",
                        "provider": name,
                        "model": model,
                        "complexity": complexity,
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
        for name, (provider, model) in self._providers.items():
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
        for name in self._providers:
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

    def usage_summary(self) -> dict[str, object]:
        self._roll_usage_month_if_needed()
        providers: dict[str, dict[str, object]] = {}
        for name, state in self._provider_state.items():
            avg_latency = int(state.latency_total_ms / state.requests) if state.requests else 0
            providers[name] = {
                "requests": state.requests,
                "tokens_in": state.tokens_in,
                "tokens_out": state.tokens_out,
                "tokens_total": state.tokens_in + state.tokens_out,
                "estimated_cost_usd": round(state.estimated_cost_usd, 6),
                "avg_latency_ms": avg_latency,
                "last_model": state.last_model or None,
            }
        return {
            "month": self._usage_month_key,
            "warning_threshold_usd": self._monthly_budget_warning_usd,
            "hard_stop_threshold_usd": self._monthly_budget_hard_stop_usd,
            "total_estimated_cost_usd": round(self._monthly_spend_usd(), 6),
            "providers": providers,
        }

    def usage_report_text(self) -> str:
        summary = self.usage_summary()
        providers = cast(dict[str, dict[str, object]], summary["providers"])
        total_spend = self._as_float(summary.get("total_estimated_cost_usd"))
        warn_threshold = self._as_float(summary.get("warning_threshold_usd"))
        hard_stop_threshold = self._as_float(summary.get("hard_stop_threshold_usd"))
        lines = [
            f"Usage ({summary['month']} UTC)",
            (
                f"Total estimated spend: ${total_spend:.4f} "
                f"(warn ${warn_threshold:.2f}, "
                f"hard-stop ${hard_stop_threshold:.2f})"
            ),
        ]
        for name in _COMPLEX_PROVIDER_ORDER:
            if name not in providers:
                continue
            row = providers[name]
            requests = self._as_int(row.get("requests"))
            tokens_in = self._as_int(row.get("tokens_in"))
            tokens_out = self._as_int(row.get("tokens_out"))
            est_cost = self._as_float(row.get("estimated_cost_usd"))
            avg_latency = self._as_int(row.get("avg_latency_ms"))
            lines.append(
                (
                    f"- {name}: req={requests}, in={tokens_in}, "
                    f"out={tokens_out}, est=${est_cost:.4f}, avg={avg_latency}ms"
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _as_int(value: object) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _as_float(value: object) -> float:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0
