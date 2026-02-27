"""Shared test fixtures."""

import os
import pytest

# Set test environment variables before importing config
os.environ.setdefault("CUE_OPENAI_API_KEY", "")
os.environ.setdefault("CUE_TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("CUE_STATE_DB_PATH", ":memory:")


@pytest.fixture
def sample_config():
    from cue_agent.config import CueConfig

    return CueConfig(
        openai_api_key="test-key",
        openai_model="gpt-4o",
        anthropic_api_key="",
        openrouter_api_key="",
        lmstudio_base_url="http://localhost:1234",
        lmstudio_model="test-local",
        telegram_bot_token="test-bot-token",
        telegram_admin_chat_id=12345,
        state_db_path="test_state.db",
        soul_md_path="SOUL.md",
        high_risk_tools=["run_shell", "write_file"],
        require_approval=True,
        loop_enabled=False,
        heartbeat_enabled=False,
    )
