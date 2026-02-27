"""Tests for configuration loading."""

from cue_agent.config import CueConfig


def test_default_config():
    config = CueConfig(openai_api_key="", telegram_bot_token="")
    assert config.openai_model == "gpt-4o"
    assert config.llm_temperature == 0.0
    assert config.loop_enabled is False
    assert config.heartbeat_enabled is False


def test_has_provider_flags():
    config = CueConfig(openai_api_key="sk-test", anthropic_api_key="", openrouter_api_key="")
    assert config.has_openai is True
    assert config.has_anthropic is False
    assert config.has_openrouter is False


def test_high_risk_tools_default():
    config = CueConfig()
    assert "run_shell" in config.high_risk_tools
