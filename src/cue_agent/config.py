"""Central configuration loaded from .env via Pydantic Settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class CueConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CUE_", extra="ignore")

    # --- Brain / LLM Providers ---
    # Primary: OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com"

    # Fallback 1: Anthropic Claude
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    # Fallback 2: OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o"
    openrouter_base_url: str = "https://openrouter.ai/api"

    # Fallback 3: LM Studio (local)
    lmstudio_base_url: str = "http://localhost:1234"
    lmstudio_model: str = "local-model"

    # LLM behavior
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 60

    # --- Communication / Telegram ---
    telegram_bot_token: str = ""
    telegram_admin_chat_id: int = 0
    telegram_webhook_url: str = ""

    # --- Memory / State ---
    state_db_path: str = "cue_state.db"
    soul_md_path: str = "SOUL.md"

    # --- Heartbeat ---
    heartbeat_enabled: bool = False
    daily_summary_cron: str = "0 8 * * *"

    # --- Security ---
    high_risk_tools: list[str] = ["run_shell", "write_file", "send_telegram"]
    require_approval: bool = True

    # --- Skills ---
    skills_dir: str = "skills"
    skills_hot_reload: bool = True

    # --- Autonomous Loop ---
    loop_enabled: bool = False
    loop_interval_seconds: int = 30

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_openrouter(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token)
