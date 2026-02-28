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
    llm_budget_warning_usd: float = 20.0
    llm_monthly_budget_usd: float = 50.0
    llm_budget_enforce_hard_stop: bool = True
    llm_cost_openai_input_per_1k: float = 0.005
    llm_cost_openai_output_per_1k: float = 0.015
    llm_cost_anthropic_input_per_1k: float = 0.003
    llm_cost_anthropic_output_per_1k: float = 0.015
    llm_cost_openrouter_input_per_1k: float = 0.003
    llm_cost_openrouter_output_per_1k: float = 0.010
    llm_cost_lmstudio_input_per_1k: float = 0.0
    llm_cost_lmstudio_output_per_1k: float = 0.0

    # --- Communication / Telegram ---
    telegram_bot_token: str = ""
    telegram_admin_chat_id: int = 0
    telegram_webhook_url: str = ""
    notifications_enabled: bool = True
    notification_delivery_mode: str = "immediate"
    notification_priority_threshold: str = "medium"
    notification_quiet_hours_start: int = 22
    notification_quiet_hours_end: int = 7
    notification_timezone: str = "UTC"
    notification_hourly_digest_cron: str = "0 * * * *"
    notification_daily_digest_cron: str = "0 8 * * *"

    # --- Memory / State ---
    state_db_path: str = "cue_state.db"
    soul_md_path: str = "SOUL.md"
    vector_memory_enabled: bool = False
    vector_memory_path: str = "data/vector_memory"
    vector_memory_collection: str = "cue_agent_memory"
    vector_memory_top_k: int = 4
    vector_memory_consolidation_enabled: bool = True
    vector_memory_consolidation_cron: str = "0 */6 * * *"
    vector_memory_consolidation_min_entries: int = 30
    vector_memory_consolidation_keep_recent: int = 20
    vector_memory_consolidation_max_items: int = 120

    # --- Web Search ---
    search_provider: str = "auto"
    search_max_results: int = 5
    search_region: str = "us-en"
    search_rate_limit_seconds: float = 1.0
    tavily_api_key: str = ""
    serpapi_api_key: str = ""

    # --- Heartbeat ---
    heartbeat_enabled: bool = False
    daily_summary_cron: str = "0 8 * * *"

    # --- Security ---
    high_risk_tools: list[str] = ["run_shell", "write_file", "send_telegram"]
    approval_required_levels: list[str] = ["high", "critical"]
    risk_rules_path: str = "skills/risk_rules.json"
    risk_sandbox_dry_run: bool = False
    require_approval: bool = True

    # --- Skills ---
    skills_dir: str = "skills"
    skills_hot_reload: bool = True

    # --- Autonomous Loop ---
    loop_enabled: bool = False
    loop_interval_seconds: int = 30
    task_queue_enabled: bool = True
    task_queue_max_list: int = 20
    task_queue_retry_failed_attempts: int = 2
    task_queue_auto_subtasks_enabled: bool = True
    task_queue_auto_subtasks_max: int = 3

    # --- Healthcheck endpoint ---
    healthcheck_enabled: bool = True
    healthcheck_host: str = "0.0.0.0"
    healthcheck_port: int = 8080
    dashboard_enabled: bool = False
    dashboard_username: str = "admin"
    dashboard_password: str = "change-me"
    dashboard_timeline_limit: int = 200

    # --- Retry / Resilience ---
    retry_tool_attempts: int = 3
    retry_telegram_attempts: int = 5
    retry_llm_attempts: int = 3
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 5.0
    retry_jitter_seconds: float = 0.2
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: int = 300

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
