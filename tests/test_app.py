"""Tests for CueApp orchestration paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import cue_agent.app as app_module
from cue_agent.brain.llm_router import LLMAllProvidersDownError
from cue_agent.comms.models import UnifiedMessage


def _install_fakes(
    monkeypatch,
    *,
    has_telegram: bool,
    heartbeat_enabled: bool = False,
    skills_hot_reload: bool = False,
    loop_enabled: bool = False,
    load_skills: bool = False,
    outage_on_chat: bool = False,
    vector_memory_enabled: bool = False,
):
    t = SimpleNamespace(
        action_registry_bots=[],
        marketplace_searches=[],
        marketplace_installs=[],
        marketplace_updates=[],
        memory_turns=[],
        vector_turns=[],
        vector_recalls=[],
        task_creates=[],
        task_subcreates=[],
        task_marks_done=[],
        task_retries=[],
        task_dependencies=[],
        task_list=[],
        skill_loader_unloads=[],
        skill_loader_reloads=[],
        action_unloads=[],
        action_reloads=[],
        approval_callbacks=[],
        notification_events=[],
        notification_flushes=[],
        heartbeat_start=0,
        heartbeat_stop=0,
        heartbeat_crons=[],
        watcher_start=0,
        watcher_stop=0,
        loop_run_forever=0,
        loop_run_once=0,
        loop_stop=0,
        created_tasks=0,
        telegram_start=0,
        telegram_webhook_start=0,
        telegram_stop=0,
        raise_reload=False,
    )

    class FakeConfig:
        def __init__(self):
            self.state_db_path = ":memory:"
            self.soul_md_path = "SOUL.md"
            self.vector_memory_enabled = vector_memory_enabled
            self.vector_memory_path = "data/vector_memory"
            self.vector_memory_collection = "cue_agent_memory"
            self.vector_memory_top_k = 4
            self.vector_memory_consolidation_enabled = True
            self.vector_memory_consolidation_cron = "0 */6 * * *"
            self.vector_memory_consolidation_min_entries = 30
            self.vector_memory_consolidation_keep_recent = 20
            self.vector_memory_consolidation_max_items = 120
            self.high_risk_tools = ["run_shell"]
            self.require_approval = True
            self.approval_required_levels = ["high", "critical"]
            self.risk_rules_path = "skills/risk_rules.json"
            self.risk_sandbox_dry_run = False
            self.skills_dir = "skills"
            self.skills_hot_reload = skills_hot_reload
            self.skills_registry_index_path = "skills/registry/index.json"
            self.skills_registry_packages_dir = "skills/registry_packages"
            self.skills_registry_state_path = "skills/.marketplace-installed.json"
            self.heartbeat_enabled = heartbeat_enabled
            self.daily_summary_cron = "0 8 * * *"
            self.telegram_admin_chat_id = 42
            self.notifications_enabled = True
            self.notification_delivery_mode = "immediate"
            self.notification_priority_threshold = "medium"
            self.notification_quiet_hours_start = 22
            self.notification_quiet_hours_end = 7
            self.notification_timezone = "UTC"
            self.notification_hourly_digest_cron = "0 * * * *"
            self.notification_daily_digest_cron = "0 8 * * *"
            self.search_provider = "auto"
            self.loop_enabled = loop_enabled
            self.loop_interval_seconds = 1
            self.task_queue_enabled = True
            self.task_queue_max_list = 20
            self.task_queue_retry_failed_attempts = 2
            self.task_queue_auto_subtasks_enabled = True
            self.task_queue_auto_subtasks_max = 3
            self.multi_agent_enabled = True
            self.multi_agent_max_concurrent = 3
            self.multi_agent_subagent_timeout_seconds = 120
            self.multi_agent_default_provider_preference = "auto"
            self.healthcheck_enabled = False
            self.healthcheck_host = "127.0.0.1"
            self.healthcheck_port = 0
            self.dashboard_enabled = False
            self.dashboard_username = "admin"
            self.dashboard_password = "change-me"
            self.dashboard_timeline_limit = 200
            self.audit_retention_days = 30
            self.audit_cleanup_cron = "15 3 * * *"
            self.retry_tool_attempts = 3
            self.retry_telegram_attempts = 5
            self.retry_llm_attempts = 3
            self.retry_base_delay_seconds = 0.01
            self.retry_max_delay_seconds = 0.02
            self.retry_jitter_seconds = 0.0
            self.circuit_breaker_failures = 3
            self.circuit_breaker_cooldown_seconds = 5
            self.telegram_bot_token = "token" if has_telegram else ""
            self.telegram_admin_user_ids = []
            self.telegram_operator_user_ids = []
            self.telegram_webhook_url = ""
            self.telegram_webhook_listen_host = "127.0.0.1"
            self.telegram_webhook_listen_port = 0
            self.telegram_webhook_path = "/telegram/webhook"
            self.telegram_webhook_secret_token = "secret"
            self.telegram_webhook_drop_pending_updates = False
            self.has_telegram = has_telegram
            self.openai_base_url = "https://api.openai.com"
            self.openai_model = "gpt-4o"
            self.llm_temperature = 0.0
            self.llm_timeout_seconds = 30
            self.llm_budget_warning_usd = 20.0
            self.llm_monthly_budget_usd = 50.0
            self.llm_budget_enforce_hard_stop = True
            self.llm_cost_openai_input_per_1k = 0.005
            self.llm_cost_openai_output_per_1k = 0.015
            self.llm_cost_anthropic_input_per_1k = 0.003
            self.llm_cost_anthropic_output_per_1k = 0.015
            self.llm_cost_openrouter_input_per_1k = 0.003
            self.llm_cost_openrouter_output_per_1k = 0.01
            self.llm_cost_lmstudio_input_per_1k = 0.0
            self.llm_cost_lmstudio_output_per_1k = 0.0
            self.multi_user_enabled = True
            self.multi_user_bootstrap_first_user = True

    class FakeStateManager:
        def __init__(self, db_path):  # noqa: ARG002
            pass

    class FakeSoulLoader:
        def __init__(self, path):  # noqa: ARG002
            pass

        def inject(self, text: str) -> str:
            return text

    class FakeRouter:
        def __init__(self, config, event_handler=None):  # noqa: ARG002
            self.event_handler = event_handler

        def health_check(self):
            return {"openai": True}

        def health_status(self):
            return {"openai": "unknown"}

        def usage_report_text(self) -> str:
            return "Usage (2026-02 UTC)\nTotal estimated spend: $0.0000"

        def usage_summary(self):
            return {
                "month": "2026-02",
                "total_estimated_cost_usd": 0.0,
                "providers": {
                    "openai": {
                        "requests": 0,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "tokens_total": 0,
                        "estimated_cost_usd": 0.0,
                        "avg_latency_ms": 0,
                        "last_model": "gpt-4o",
                    }
                },
            }

    class FakeBrain:
        def __init__(self, config, soul_loader, router):  # noqa: ARG002
            self.router = router

        def chat(self, user_input: str, extra_context: str = "") -> str:
            if outage_on_chat:
                raise LLMAllProvidersDownError({"openai": "down", "lmstudio": "down"})
            return f"reply:{extra_context}|{user_input}"

        def plan(self, task, manifest, memory_context=""):  # noqa: ARG002
            return SimpleNamespace(steps=[])

    class FakeMemory:
        def __init__(self, state_manager):  # noqa: ARG002
            pass

        def add_turn(self, chat_id: str, role: str, content: str, run_id: str | None = None):
            t.memory_turns.append((chat_id, role, content, run_id))

        def get_context(self, chat_id: str, limit: int = 20) -> str:  # noqa: ARG002
            return "ctx"

    class FakeVectorMemory:
        def __init__(self, config):
            self._enabled = config.vector_memory_enabled
            self.is_available = config.vector_memory_enabled

        def add_turn(self, chat_id: str, role: str, content: str, run_id: str | None = None):
            if not self._enabled:
                return
            t.vector_turns.append((chat_id, role, content, run_id))

        def recall_as_context(self, chat_id: str, query: str, limit: int | None = None):
            if not self._enabled:
                return ""
            t.vector_recalls.append((chat_id, query, limit))
            return "Long-term semantic memory:\n- vector_ctx"

    class FakeTaskQueue:
        def __init__(self, db_path):  # noqa: ARG002
            pass

        def list_tasks(self, status=None, limit=20):  # noqa: ARG002
            if status is None:
                return list(t.task_list)
            return [task for task in t.task_list if task["status"] == status]

        def create_task(self, title, description="", priority=3, parent_task_id=None, source="user", depends_on=None):  # noqa: ANN001, ARG002
            task_id = len(t.task_list) + 1
            row = {
                "id": task_id,
                "title": title,
                "description": description,
                "priority": priority,
                "status": "pending",
                "depends_on": list(depends_on or []),
                "parent_task_id": parent_task_id,
                "source": source,
            }
            t.task_list.append(row)
            t.task_creates.append(row)
            return task_id

        def create_subtask(self, parent_task_id, title, description="", priority=3, source="agent_subtask"):  # noqa: ANN001, ARG002
            task_id = len(t.task_list) + 1
            row = {
                "id": task_id,
                "title": title,
                "description": description,
                "priority": priority,
                "status": "pending",
                "depends_on": [],
                "parent_task_id": parent_task_id,
                "source": source,
            }
            t.task_list.append(row)
            t.task_subcreates.append(row)
            return task_id

        def mark_done(self, task_id):
            t.task_marks_done.append(task_id)
            for row in t.task_list:
                if row["id"] == task_id:
                    row["status"] = "done"

        def add_dependency(self, task_id, depends_on_task_id):
            t.task_dependencies.append((task_id, depends_on_task_id))

        def retry_task(self, task_id):
            t.task_retries.append(task_id)

        def queue_stats(self):
            return {"pending": 0, "blocked": 0, "in_progress": 0, "failed": 0, "done": 0, "canceled": 0, "total": 0}

    class FakeActionRegistry:
        def __init__(self, telegram_bot=None, tool_event_handler=None):  # noqa: ARG002
            t.action_registry_bots.append(telegram_bot)
            self.eap_registry = object()
            self._skill_names = []

        def load_skills(self, skills):
            self._skill_names = list(skills.keys())
            return self._skill_names

        def unload_skill(self, name):
            t.action_unloads.append(name)

        def reload_skill(self, skill):
            t.action_reloads.append(skill.name)

        def get_hashed_manifest(self):
            return {"hash": "run_shell"}

        @property
        def tool_count(self):
            return 5

        @property
        def skill_names(self):
            return list(self._skill_names)

    class FakeExecutor:
        def __init__(self, state_manager, eap_registry):  # noqa: ARG002
            pass

    class FakeRiskClassifier:
        def __init__(self, tools, **kwargs):  # noqa: ARG002
            pass

        def assess(self, tool_name, arguments=None, **kwargs):  # noqa: ANN001, ARG002
            return SimpleNamespace(level="low", reason=f"{tool_name} default")

    class FakeApprovalGate:
        def __init__(self, classifier, approval_gateway=None, tool_name_lookup=None, risk_event_handler=None):  # noqa: ARG002
            pass

    class FakeNotificationManager:
        def __init__(self, config, bot, admin_chat_id):  # noqa: ARG002
            pass

        def emit(self, category, priority, title, body, metadata=None):  # noqa: ANN001
            t.notification_events.append(
                {
                    "category": category,
                    "priority": priority,
                    "title": title,
                    "body": body,
                    "metadata": metadata or {},
                }
            )

        async def flush(self, force=False, batched=False):  # noqa: ANN001
            t.notification_flushes.append({"force": force, "batched": batched})
            return 1

        def queue_size(self) -> int:
            return 0

        def event_counters(self) -> dict[str, int]:
            return {"task_completion": 1}

        def recent_errors(self, limit: int = 5):  # noqa: ARG002
            return ["error-1"]

    class FakeHeartbeat:
        def __init__(self, config):  # noqa: ARG002
            pass

        async def start(self):
            t.heartbeat_start += 1

        async def add_cron_task(self, task_id, func, cron_expr):  # noqa: ARG002
            t.heartbeat_crons.append(task_id)

        async def stop(self):
            t.heartbeat_stop += 1

    class FakeBot:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_message(self, **kwargs):  # noqa: ARG002
            self.sent.append(kwargs)
            return None

    class FakeTelegramGateway:
        def __init__(self, config, on_message, on_approval):  # noqa: ARG002
            self.app = SimpleNamespace(bot=FakeBot())
            self.on_message = on_message
            self.on_approval = on_approval

        async def start_polling(self):
            t.telegram_start += 1

        async def start_webhook(self):
            t.telegram_webhook_start += 1

        def webhook_diagnostics(self):
            return {
                "configured_path": "/telegram/webhook",
                "registered": False,
                "request_count": 0,
                "rejected_count": 0,
            }

        async def stop(self):
            t.telegram_stop += 1

    class FakeApprovalGateway:
        def __init__(self, bot, admin_chat_id):  # noqa: ARG002
            pass

        async def handle_callback(self, approval_id: str, approved: bool):
            t.approval_callbacks.append((approval_id, approved))

        async def request_approval(self, action_description: str, step_id: str):  # noqa: ARG002
            return True

        def pending_approvals(self):
            return [
                {
                    "approval_id": "approval_step_1",
                    "step_id": "step-1",
                    "action_description": "Run dangerous command",
                }
            ]

    class FakeSkillLoader:
        def __init__(self, skills_dir):  # noqa: ARG002
            pass

        def load_all(self):
            if load_skills:
                return {"pack": SimpleNamespace(name="pack", tools=[1])}
            return {}

        def unload_skill(self, name: str):
            t.skill_loader_unloads.append(name)

        def reload_skill(self, path: Path):
            if t.raise_reload:
                raise RuntimeError("reload failed")
            skill = SimpleNamespace(name=path.stem or "pack", tools=[1, 2])
            t.skill_loader_reloads.append(skill.name)
            return skill

    class FakeSkillWatcher:
        def __init__(self, skills_dir, on_change):  # noqa: ARG002
            self.on_change = on_change

        async def start(self):
            t.watcher_start += 1
            await asyncio.sleep(0)

        def stop(self):
            t.watcher_stop += 1

    class FakeMarketplace:
        def __init__(self, **kwargs):  # noqa: ARG002
            pass

        def search(self, query: str = "", limit: int = 10):  # noqa: ARG002
            t.marketplace_searches.append(query)
            return [
                {
                    "id": "release_digest",
                    "latest_version": "1.1.0",
                    "quality_score": 0.95,
                    "usage_count": 500,
                    "description": "release digest",
                }
            ]

        def install(self, skill_id: str, *, version: str | None = None, force: bool = False):  # noqa: ARG002
            t.marketplace_installs.append((skill_id, version))
            path = Path("/tmp") / f"{skill_id}.py"
            return {"skill_id": skill_id, "version": version or "1.1.0", "path": str(path)}

        def update(self, skill_id: str):
            t.marketplace_updates.append(skill_id)
            return {"skill_id": skill_id, "status": "updated", "previous_version": "1.0.0", "version": "1.1.0"}

        def update_all(self):
            t.marketplace_updates.append("all")
            return [{"skill_id": "release_digest", "status": "up_to_date", "version": "1.1.0"}]

        def validate_submission(self, _path: str):
            return {"ok": True, "errors": [], "warnings": [], "skill_name": "x"}

        def validate_registry_index(self):
            return {"ok": True, "errors": [], "warnings": [], "skill_count": 1}

    class FakeRalphLoop:
        def __init__(self, **kwargs):  # noqa: ARG002
            self.is_running = False
            self.last_iteration_time = None
            self.current_task = None

        async def run_forever(self):
            t.loop_run_forever += 1
            self.is_running = True
            await asyncio.sleep(0)

        async def run_once(self):
            t.loop_run_once += 1
            self.last_iteration_time = "2026-02-27T00:00:00+00:00"
            await asyncio.sleep(0)

        def stop(self):
            t.loop_stop += 1
            self.is_running = False

    monkeypatch.setattr(app_module, "CueConfig", FakeConfig)
    monkeypatch.setattr(app_module, "StateManager", FakeStateManager)
    monkeypatch.setattr(app_module, "SoulLoader", FakeSoulLoader)
    monkeypatch.setattr(app_module, "LLMRouter", FakeRouter)
    monkeypatch.setattr(app_module, "CueBrain", FakeBrain)
    monkeypatch.setattr(app_module, "SessionMemory", FakeMemory)
    monkeypatch.setattr(app_module, "VectorMemory", FakeVectorMemory)
    monkeypatch.setattr(app_module, "TaskQueue", FakeTaskQueue)
    monkeypatch.setattr(app_module, "ActionRegistry", FakeActionRegistry)
    monkeypatch.setattr(app_module, "AsyncLocalExecutor", FakeExecutor)
    monkeypatch.setattr(app_module, "RiskClassifier", FakeRiskClassifier)
    monkeypatch.setattr(app_module, "ApprovalGate", FakeApprovalGate)
    monkeypatch.setattr(app_module, "Heartbeat", FakeHeartbeat)
    monkeypatch.setattr(app_module, "TelegramGateway", FakeTelegramGateway)
    monkeypatch.setattr(app_module, "ApprovalGateway", FakeApprovalGateway)
    monkeypatch.setattr(app_module, "NotificationManager", FakeNotificationManager)
    monkeypatch.setattr(app_module, "SkillLoader", FakeSkillLoader)
    monkeypatch.setattr(app_module, "SkillWatcher", FakeSkillWatcher)
    monkeypatch.setattr(app_module, "SkillMarketplace", FakeMarketplace)
    monkeypatch.setattr(app_module, "RalphLoop", FakeRalphLoop)

    return t


@pytest.mark.asyncio
async def test_app_init_without_telegram_and_handle_message(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=False, load_skills=True)
    app = app_module.CueApp()

    msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="hello")
    response = await app._handle_message(msg)

    assert app.telegram is None
    assert response.chat_id == "chat-1"
    assert response.text == "reply:ctx|hello"
    assert t.memory_turns[0][0] == "telegram:chat-1:u1"
    assert [turn[1] for turn in t.memory_turns] == ["user", "assistant"]
    assert t.vector_turns == []
    assert t.vector_recalls == []
    assert t.action_registry_bots == [None]
    rows = app.audit_trail.query(app_module.AuditQuery(limit=5))
    assert rows[0]["user_id"] == "u1"
    health = app._build_health_status()
    assert health["status"] == "ok"
    assert health["providers"] == {"openai": "unknown"}
    assert health["memory"] == {"vector_enabled": False, "vector_available": False}
    assert health["notifications"]["enabled"] is True
    assert health["agents"]["enabled"] is True


def test_app_dashboard_snapshot_and_timeline(monkeypatch):
    _install_fakes(monkeypatch, has_telegram=False)
    app = app_module.CueApp()

    app._handle_tool_event(
        {
            "tool_name": "read_file",
            "arguments": {"path": "README.md"},
            "duration_ms": 42,
            "outcome": "success",
        }
    )

    snapshot = app._build_dashboard_snapshot()
    assert snapshot["runtime"]["status"] == "stopped"
    assert "uptime_human" in snapshot["runtime"]
    assert isinstance(snapshot["tasks"], list)
    assert len(snapshot["actions"]) == 1
    assert snapshot["actions"][0]["tool_name"] == "read_file"
    assert snapshot["actions"][0]["risk_level"] == "low"


@pytest.mark.asyncio
async def test_app_includes_vector_context_when_enabled(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=False, vector_memory_enabled=True)
    app = app_module.CueApp()

    msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="hello")
    response = await app._handle_message(msg)

    assert "Long-term semantic memory" in response.text
    assert t.vector_recalls == [("telegram:chat-1:u1", "hello", None)]
    assert [turn[1] for turn in t.vector_turns] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_app_task_commands(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=False)
    app = app_module.CueApp()

    add_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/task add p2 Finish docs")
    add_response = await app._handle_message(add_msg)
    assert "Created task #1" in add_response.text
    assert t.task_creates[0]["priority"] == 2

    sub_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/task sub 1 p3 Add examples")
    sub_response = await app._handle_message(sub_msg)
    assert "Created sub-task #2 under #1" in sub_response.text
    assert t.task_subcreates[0]["parent_task_id"] == 1

    list_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/tasks")
    list_response = await app._handle_message(list_msg)
    assert "Task Queue:" in list_response.text
    assert "#1 [pending] p2 Finish docs" in list_response.text

    done_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/task done 1")
    done_response = await app._handle_message(done_msg)
    assert "Marked task #1 as done" == done_response.text
    assert t.task_marks_done == [1]

    usage_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/usage")
    usage_response = await app._handle_message(usage_msg)
    assert "Total estimated spend" in usage_response.text

    status_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/status")
    status_response = await app._handle_message(status_msg)
    assert "CueAgent Status" in status_response.text
    assert status_response.ui_mode == "status"

    agents_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/agents")
    agents_response = await app._handle_message(agents_msg)
    assert "Multi-Agent Orchestration" in agents_response.text
    assert agents_response.ui_mode == "status"

    skills_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/skills")
    skills_response = await app._handle_message(skills_msg)
    assert "Skills" in skills_response.text
    assert skills_response.ui_mode == "skills"

    settings_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/settings")
    settings_response = await app._handle_message(settings_msg)
    assert "Settings Snapshot" in settings_response.text
    assert settings_response.ui_mode == "settings"

    approve_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/approve")
    approve_response = await app._handle_message(approve_msg)
    assert "Approval gateway not configured." in approve_response.text
    assert approve_response.ui_mode == "approve"

    download_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/tasks download")
    download_response = await app._handle_message(download_msg)
    assert download_response.document_filename == "cue-agent-tasks.json"
    assert download_response.document_bytes is not None

    audit_msg = UnifiedMessage(
        platform="telegram", chat_id="chat-1", user_id="u1", text="/audit json event=conversation"
    )
    audit_response = await app._handle_message(audit_msg)
    assert "Exported" in audit_response.text
    assert audit_response.document_filename is not None
    assert audit_response.document_filename.endswith(".json")
    assert audit_response.document_bytes is not None

    file_msg = UnifiedMessage(
        platform="telegram",
        chat_id="chat-1",
        user_id="u1",
        text="/file",
        raw={
            "attachment": {
                "type": "document",
                "file_name": "report.txt",
                "mime_type": "text/plain",
            }
        },
    )
    file_response = await app._handle_message(file_msg)
    assert "File Received" in file_response.text

    market_search_msg = UnifiedMessage(
        platform="telegram", chat_id="chat-1", user_id="u1", text="/market search release"
    )
    market_search_response = await app._handle_message(market_search_msg)
    assert "Marketplace Skills" in market_search_response.text


@pytest.mark.asyncio
async def test_app_users_command_permissions(monkeypatch):
    _install_fakes(monkeypatch, has_telegram=False)
    app = app_module.CueApp()

    me_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/users me")
    me_response = await app._handle_message(me_msg)
    assert "User Profile" in me_response.text
    assert "u1" in me_response.text

    denied_msg = UnifiedMessage(
        platform="telegram",
        chat_id="chat-1",
        user_id="u1",
        text="/users role u2 operator",
    )
    denied_response = await app._handle_message(denied_msg)
    assert "Access denied" in denied_response.text

    denied_market_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="/market install x")
    denied_market_response = await app._handle_message(denied_market_msg)
    assert "Access denied" in denied_market_response.text


@pytest.mark.asyncio
async def test_app_marketplace_install_and_update_admin(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=False)
    app = app_module.CueApp()
    app.user_access.set_role("u-admin", "admin", actor_user_id="system")

    install_msg = UnifiedMessage(
        platform="telegram", chat_id="chat-1", user_id="u-admin", text="/market install release_digest"
    )
    install_response = await app._handle_message(install_msg)
    assert "Installed `release_digest`" in install_response.text
    assert t.marketplace_installs == [("release_digest", None)]
    assert t.skill_loader_reloads[-1] == "release_digest"
    assert t.action_reloads[-1] == "release_digest"

    update_msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u-admin", text="/market update all")
    update_response = await app._handle_message(update_msg)
    assert "Marketplace Updates" in update_response.text
    assert t.marketplace_updates == ["all"]


@pytest.mark.asyncio
async def test_app_blocks_readonly_task_mutations(monkeypatch):
    _install_fakes(monkeypatch, has_telegram=False)
    app = app_module.CueApp()
    app.user_access.set_role("u-read", "readonly", actor_user_id="admin")

    msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u-read", text="/task add p2 Do x")
    response = await app._handle_message(msg)
    assert "Access denied" in response.text


@pytest.mark.asyncio
async def test_app_telegram_branch_approval_and_skill_change(monkeypatch, tmp_path: Path):
    t = _install_fakes(monkeypatch, has_telegram=True)
    app = app_module.CueApp()

    assert app.telegram is not None
    assert len(t.action_registry_bots) == 2

    await app._handle_approval("approval-1", True)
    assert t.approval_callbacks == [("approval-1", True)]

    deleted_path = tmp_path / "dead_skill.py"
    await app._handle_skill_change(deleted_path, "deleted")
    assert t.skill_loader_unloads == ["dead_skill"]
    assert t.action_unloads == ["dead_skill"]

    created_path = tmp_path / "new_skill.py"
    await app._handle_skill_change(created_path, "created")
    assert t.skill_loader_reloads[-1] == "new_skill"
    assert t.action_reloads[-1] == "new_skill"

    t.raise_reload = True
    await app._handle_skill_change(created_path, "modified")

    app._handle_router_event(
        {
            "event": "llm_budget_warning",
            "monthly_spend_usd": 21.0,
            "warning_threshold_usd": 20.0,
            "hard_stop_threshold_usd": 50.0,
        }
    )
    app._handle_risk_event(
        {
            "event": "high_risk_action",
            "tool_name": "run_shell",
            "risk_level": "critical",
            "reason": "destructive command",
        }
    )
    assert any(event["category"] == "budget_warning" for event in t.notification_events)
    assert any(event["category"] == "high_risk_action" for event in t.notification_events)


@pytest.mark.asyncio
async def test_app_approval_routing_operator_and_admin_only(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=True)
    app = app_module.CueApp()

    actor_user = UnifiedMessage(platform="telegram", chat_id="42", user_id="u-user", text="approve")
    app.user_access.set_role("u-user", "user", actor_user_id="admin")
    denied = await app._handle_approval("approval-1", True, actor_user)
    assert denied is False
    assert t.approval_callbacks == []

    actor_operator = UnifiedMessage(platform="telegram", chat_id="42", user_id="u-op", text="approve")
    app.user_access.set_role("u-op", "operator", actor_user_id="admin")
    allowed = await app._handle_approval("approval-2", True, actor_operator)
    assert allowed is True
    assert t.approval_callbacks == [("approval-2", True)]


@pytest.mark.asyncio
async def test_app_start_once_with_heartbeat_and_hot_reload(monkeypatch):
    t = _install_fakes(
        monkeypatch,
        has_telegram=True,
        heartbeat_enabled=True,
        skills_hot_reload=True,
    )
    app = app_module.CueApp()

    await app.start(mode="once")

    assert t.heartbeat_start == 1
    assert t.heartbeat_crons == ["daily_summary", "health_check", "notification_digest", "audit_retention_cleanup"]
    assert t.loop_run_once == 1
    assert t.watcher_stop == 1
    assert t.loop_stop == 1
    assert t.heartbeat_stop == 1
    assert t.telegram_stop == 1
    assert t.notification_flushes[-1] == {"force": True, "batched": True}


@pytest.mark.asyncio
async def test_app_start_schedules_vector_consolidation(monkeypatch):
    t = _install_fakes(
        monkeypatch,
        has_telegram=False,
        heartbeat_enabled=True,
        vector_memory_enabled=True,
    )
    app = app_module.CueApp()

    await app.start(mode="once")

    assert t.heartbeat_start == 1
    assert t.heartbeat_crons == ["vector_memory_consolidation", "audit_retention_cleanup"]


@pytest.mark.asyncio
async def test_run_polling_paths(monkeypatch):
    t1 = _install_fakes(monkeypatch, has_telegram=False)
    app1 = app_module.CueApp()
    await app1._run_polling()
    assert t1.telegram_start == 0

    t2 = _install_fakes(monkeypatch, has_telegram=True, loop_enabled=True)
    app2 = app_module.CueApp()

    class _FakeEvent:
        def set(self):
            return None

        async def wait(self):
            return None

    class _FakeLoop:
        def add_signal_handler(self, sig, handler):  # noqa: ARG002
            raise NotImplementedError

    monkeypatch.setattr(app_module.asyncio, "Event", _FakeEvent)
    monkeypatch.setattr(app_module.asyncio, "get_event_loop", lambda: _FakeLoop())
    original_create_task = app_module.asyncio.create_task

    def _capture_task(coro):
        t2.created_tasks += 1
        return original_create_task(coro)

    monkeypatch.setattr(app_module.asyncio, "create_task", _capture_task)

    await app2._run_polling()
    assert t2.telegram_start == 1
    assert t2.created_tasks >= 1


@pytest.mark.asyncio
async def test_run_webhook_paths(monkeypatch):
    t1 = _install_fakes(monkeypatch, has_telegram=False)
    app1 = app_module.CueApp()
    await app1._run_webhook()
    assert t1.telegram_webhook_start == 0

    t2 = _install_fakes(monkeypatch, has_telegram=True, loop_enabled=True)
    app2 = app_module.CueApp()

    class _FakeEvent:
        def set(self):
            return None

        async def wait(self):
            return None

    class _FakeLoop:
        def add_signal_handler(self, sig, handler):  # noqa: ARG002
            raise NotImplementedError

    monkeypatch.setattr(app_module.asyncio, "Event", _FakeEvent)
    monkeypatch.setattr(app_module.asyncio, "get_event_loop", lambda: _FakeLoop())
    original_create_task = app_module.asyncio.create_task

    def _capture_task(coro):
        t2.created_tasks += 1
        return original_create_task(coro)

    monkeypatch.setattr(app_module.asyncio, "create_task", _capture_task)

    await app2._run_webhook()
    assert t2.telegram_webhook_start == 1
    assert t2.created_tasks >= 1


@pytest.mark.asyncio
async def test_app_queues_messages_when_all_providers_down(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=True, outage_on_chat=True)
    app = app_module.CueApp()
    msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="hello")

    response = await app._handle_message(msg)
    assert "temporarily unavailable" in response.text
    assert len(app._queued_messages) == 1

    # Outage notification emitted once.
    assert len(t.notification_events) == 1
    assert t.notification_events[0]["category"] == "outage"
    assert t.notification_events[0]["priority"] == "critical"

    # Second message should queue but avoid duplicate outage spam.
    _ = await app._handle_message(msg)
    assert len(app._queued_messages) == 2
    assert len(t.notification_events) == 1
