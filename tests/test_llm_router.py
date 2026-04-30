"""Tests for LLMRouter fallback behavior."""

from __future__ import annotations

import logging

from eap.agent.providers.base import CompletionRequest, CompletionResponse, ProviderMessage

from cue_agent.brain.llm_router import LLMRouter
from cue_agent.config import CueConfig


class _FakeProvider:
    def __init__(self, name: str, fail_complete: bool = False, usage: dict[str, int] | None = None):
        self.name = name
        self.fail_complete = fail_complete
        self.usage = usage or {}
        self.last_model: str | None = None
        self.calls = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        self.last_model = request.model
        if self.fail_complete:
            raise RuntimeError(f"{self.name} unavailable")
        payload = {"usage": self.usage} if self.usage else {}
        return CompletionResponse(text=f"ok:{self.name}", raw_response=payload)

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


def _complex_request() -> CompletionRequest:
    return CompletionRequest(
        model="ignored",
        messages=[ProviderMessage(role="user", content="Analyze architecture tradeoffs for this multi-step task.")],
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

    response = router.complete(_complex_request())
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
        _ = router.complete(_complex_request())

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

        def complete(self, request: CompletionRequest) -> CompletionResponse:
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
    first = router.complete(_complex_request())
    assert first.text == "ok:lmstudio"
    assert failing.calls == 1

    # Second call should skip openai while circuit is open.
    second = router.complete(_complex_request())
    assert second.text == "ok:lmstudio"
    assert failing.calls == 1


def test_llm_router_routes_simple_requests_to_cheaper_provider(monkeypatch):
    openai = _FakeProvider("openai")
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return openai
        return lmstudio

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

    response = router.complete(_request())
    assert response.text == "ok:lmstudio"
    assert lmstudio.calls == 1
    assert openai.calls == 0


def test_llm_router_routes_complex_requests_to_strong_provider(monkeypatch):
    openai = _FakeProvider("openai")
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return openai
        return lmstudio

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
    request = CompletionRequest(
        model="ignored",
        messages=[ProviderMessage(role="user", content="Analyze architecture tradeoffs for this multi-step design.")],
        temperature=0.0,
    )

    response = router.complete(request)
    assert response.text == "ok:openai"
    assert openai.calls == 1
    assert lmstudio.calls == 0


def test_llm_router_provider_preference_overrides_order(monkeypatch):
    openai = _FakeProvider("openai")
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return openai
        return lmstudio

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

    with router.provider_preference("openai"):
        response = router.complete(_request())

    assert response.text == "ok:openai"
    assert openai.calls == 1
    assert lmstudio.calls == 0


def test_llm_router_usage_summary_tracks_estimated_cost(monkeypatch):
    openai = _FakeProvider("openai", usage={"prompt_tokens": 1000, "completion_tokens": 500})
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return openai
        return lmstudio

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
    request = CompletionRequest(
        model="ignored",
        messages=[ProviderMessage(role="user", content="Analyze this design deeply with tradeoffs.")],
        temperature=0.0,
    )

    _ = router.complete(request)
    summary = router.usage_summary()
    providers = summary["providers"]
    assert isinstance(providers, dict)
    openai_usage = providers["openai"]
    assert openai_usage["requests"] == 1
    assert openai_usage["tokens_in"] == 1000
    assert openai_usage["tokens_out"] == 500
    assert openai_usage["estimated_cost_usd"] > 0.0
    text = router.usage_report_text()
    assert "Total estimated spend" in text
    assert "- openai:" in text


def test_llm_router_budget_hard_stop_skips_remote_provider(monkeypatch):
    openai = _FakeProvider("openai", usage={"prompt_tokens": 500, "completion_tokens": 500})
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return openai
        return lmstudio

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)
    router = LLMRouter(
        CueConfig(
            openai_api_key="sk-test",
            lmstudio_base_url="http://localhost:1234",
            llm_budget_warning_usd=0.0,
            llm_monthly_budget_usd=0.001,
            llm_budget_enforce_hard_stop=True,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            retry_jitter_seconds=0.0,
        )
    )
    request = CompletionRequest(
        model="ignored",
        messages=[ProviderMessage(role="user", content="Analyze architecture deeply and compare designs.")],
        temperature=0.0,
    )

    first = router.complete(request)
    assert first.text == "ok:openai"

    second = router.complete(request)
    assert second.text == "ok:lmstudio"
    assert openai.calls == 1


def test_llm_router_emits_budget_events(monkeypatch):
    openai = _FakeProvider("openai", usage={"prompt_tokens": 1000, "completion_tokens": 1000})
    lmstudio = _FakeProvider("lmstudio")

    def fake_create_provider(provider_name: str, base_url: str, api_key: str, timeout_seconds: int):  # noqa: ARG001
        if provider_name == "openai":
            return openai
        return lmstudio

    monkeypatch.setattr("cue_agent.brain.llm_router.create_provider", fake_create_provider)
    events: list[dict] = []
    router = LLMRouter(
        CueConfig(
            openai_api_key="sk-test",
            lmstudio_base_url="http://localhost:1234",
            llm_budget_warning_usd=0.001,
            llm_monthly_budget_usd=0.002,
            llm_budget_enforce_hard_stop=True,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            retry_jitter_seconds=0.0,
        ),
        event_handler=lambda event: events.append(event),
    )
    request = CompletionRequest(
        model="ignored",
        messages=[ProviderMessage(role="user", content="Analyze architecture deeply and compare designs.")],
        temperature=0.0,
    )

    _ = router.complete(request)
    _ = router.complete(request)

    names = [event["event"] for event in events]
    assert "llm_budget_warning" in names
    assert "llm_budget_hard_stop" in names
