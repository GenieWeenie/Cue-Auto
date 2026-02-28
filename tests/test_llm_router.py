"""Tests for LLMRouter fallback behavior."""

from __future__ import annotations

import logging

from agent.providers.base import CompletionRequest, CompletionResponse, ProviderMessage

from cue_agent.brain.llm_router import LLMRouter
from cue_agent.config import CueConfig


class _FakeProvider:
    def __init__(self, name: str, fail_complete: bool = False):
        self.name = name
        self.fail_complete = fail_complete
        self.last_model: str | None = None

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.last_model = request.model
        if self.fail_complete:
            raise RuntimeError(f"{self.name} unavailable")
        return CompletionResponse(text=f"ok:{self.name}")

    def complete_with_tools(self, request: CompletionRequest) -> CompletionResponse:
        return self.complete(request)

    def stream(self, request: CompletionRequest):
        self.last_model = request.model
        if self.fail_complete:
            raise RuntimeError(f"{self.name} unavailable")
        return iter([f"chunk:{self.name}"])


def _request() -> CompletionRequest:
    return CompletionRequest(
        model="ignored",
        messages=[ProviderMessage(role="user", content="hello")],
        temperature=0.0,
    )


def test_llm_router_falls_back_to_anthropic(monkeypatch):
    providers: dict[str, _FakeProvider] = {}

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):
        del api_key, timeout_seconds
        if provider_name == "openai" and "openrouter" not in base_url:
            providers["openai"] = _FakeProvider("openai", fail_complete=True)
            return providers["openai"]
        if provider_name == "anthropic":
            providers["anthropic"] = _FakeProvider("anthropic")
            return providers["anthropic"]
        if provider_name == "openai" and "openrouter" in base_url:
            providers["openrouter"] = _FakeProvider("openrouter")
            return providers["openrouter"]
        providers["lmstudio"] = _FakeProvider("lmstudio")
        return providers["lmstudio"]

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)

    config = CueConfig(
        openai_api_key="sk-test",
        anthropic_api_key="ak-test",
        openrouter_api_key="",
        lmstudio_base_url="http://localhost:1234",
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        retry_jitter_seconds=0.0,
    )
    router = LLMRouter(config)

    response = router.complete(_request())
    assert response.text == "ok:anthropic"
    assert providers["openai"].last_model == config.openai_model
    assert providers["anthropic"].last_model == config.anthropic_model


def test_llm_router_raises_when_all_providers_fail(monkeypatch):
    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):
        del provider_name, base_url, api_key, timeout_seconds
        return _FakeProvider("any", fail_complete=True)

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)

    config = CueConfig(
        openai_api_key="sk-test",
        anthropic_api_key="ak-test",
        openrouter_api_key="or-test",
        lmstudio_base_url="http://localhost:1234",
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        retry_jitter_seconds=0.0,
    )
    router = LLMRouter(config)

    try:
        router.complete(_request())
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("expected RuntimeError from fallback chain")


def test_llm_router_health_check(monkeypatch):
    openai = _FakeProvider("openai", fail_complete=True)
    anthropic = _FakeProvider("anthropic")
    openrouter = _FakeProvider("openrouter")
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):
        del api_key, timeout_seconds
        if provider_name == "openai" and "openrouter" not in base_url:
            return openai
        if provider_name == "anthropic":
            return anthropic
        if provider_name == "openai" and "openrouter" in base_url:
            return openrouter
        return lmstudio

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)

    config = CueConfig(
        openai_api_key="sk-test",
        anthropic_api_key="ak-test",
        openrouter_api_key="or-test",
        lmstudio_base_url="http://localhost:1234",
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        retry_jitter_seconds=0.0,
    )
    router = LLMRouter(config)

    health = router.health_check()
    assert health["openai"] is False
    assert health["anthropic"] is True
    assert health["openrouter"] is True
    assert health["lmstudio"] is True
    status = router.health_status()
    assert status["openai"] == "down"
    assert status["anthropic"] == "up"
    assert status["openrouter"] == "up"
    assert status["lmstudio"] == "up"


def test_llm_router_logs_call_metrics(monkeypatch, caplog):
    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        provider = _FakeProvider(provider_name)

        def _complete(request: CompletionRequest) -> CompletionResponse:
            return CompletionResponse(
                text="ok",
                raw_response={"usage": {"prompt_tokens": 11, "completion_tokens": 7}},
            )

        provider.complete = _complete  # type: ignore[method-assign]
        provider.complete_with_tools = _complete  # type: ignore[method-assign]
        return provider

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)
    router = LLMRouter(
        CueConfig(
            openai_api_key="sk-test",
            lmstudio_base_url="http://localhost:1234",
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            retry_jitter_seconds=0.0,
        )
    )

    with caplog.at_level(logging.INFO):
        _ = router.complete(_request())

    record = next(r for r in caplog.records if getattr(r, "event", "") == "llm_call")
    assert record.provider == "openai"
    assert record.model == "gpt-4o"
    assert record.tokens_in == 11
    assert record.tokens_out == 7
    assert isinstance(record.latency_ms, int)


def test_llm_router_circuit_breaker_skips_open_provider(monkeypatch):
    class _AlwaysFailProvider(_FakeProvider):
        def __init__(self):
            super().__init__("openai", fail_complete=True)
            self.calls = 0

        def complete(self, request: CompletionRequest) -> CompletionResponse:
            self.calls += 1
            return super().complete(request)

    failing = _AlwaysFailProvider()
    fallback = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return failing
        return fallback

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)

    config = CueConfig(
        openai_api_key="sk-test",
        lmstudio_base_url="http://localhost:1234",
        retry_llm_attempts=1,
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        retry_jitter_seconds=0.0,
        circuit_breaker_failures=1,
        circuit_breaker_cooldown_seconds=300,
    )
    router = LLMRouter(config)

    # First call fails on openai and falls back to lmstudio; openai circuit opens.
    first = router.complete(_request())
    assert first.text == "ok:lmstudio"
    assert failing.calls == 1

    # Second call should skip openai while circuit is open.
    second = router.complete(_request())
    assert second.text == "ok:lmstudio"
    assert failing.calls == 1
