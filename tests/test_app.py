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
            self.approval_required_levels = ["high", "critical"]
            self.risk_rules_path = "skills/risk_rules.json"
            self.risk_sandbox_dry_run = False
            self.skills_dir = "skills"
            self.skills_hot_reload = skills_hot_reload
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
            self.loop_enabled = loop_enabled
            self.loop_interval_seconds = 1
            self.task_queue_enabled = True
            self.task_queue_max_list = 20
            self.task_queue_retry_failed_attempts = 2
            self.task_queue_auto_subtasks_enabled = True
            self.task_queue_auto_subtasks_max = 3
            self.healthcheck_enabled = False
            self.healthcheck_host = "127.0.0.1"
            self.healthcheck_port = 0
            self.retry_tool_attempts = 3
            self.retry_telegram_attempts = 5
            self.retry_llm_attempts = 3
            self.retry_base_delay_seconds = 0.01
            self.retry_max_delay_seconds = 0.02
            self.retry_jitter_seconds = 0.0
            self.circuit_breaker_failures = 3
            self.circuit_breaker_cooldown_seconds = 5
            self.telegram_bot_token = "token" if has_telegram else ""
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
        def __init__(self, telegram_bot=None):
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

        async def stop(self):
            t.telegram_stop += 1

    class FakeApprovalGateway:
        def __init__(self, bot, admin_chat_id):  # noqa: ARG002
            pass

        async def handle_callback(self, approval_id: str, approved: bool):
            t.approval_callbacks.append((approval_id, approved))

        async def request_approval(self, action_description: str, step_id: str):  # noqa: ARG002
            return True

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

    class FakeRalphLoop:
        def __init__(self, **kwargs):  # noqa: ARG002
            self.is_running = False
            self.last_iteration_time = None

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
    assert [turn[1] for turn in t.memory_turns] == ["user", "assistant"]
    assert t.vector_turns == []
    assert t.vector_recalls == []
    assert t.action_registry_bots == [None]
    health = app._build_health_status()
    assert health["status"] == "ok"
    assert health["providers"] == {"openai": "unknown"}
    assert health["memory"] == {"vector_enabled": False, "vector_available": False}
    assert health["notifications"]["enabled"] is True


@pytest.mark.asyncio
async def test_app_includes_vector_context_when_enabled(monkeypatch):
    t = _install_fakes(monkeypatch, has_telegram=False, vector_memory_enabled=True)
    app = app_module.CueApp()

    msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="hello")
    response = await app._handle_message(msg)

    assert "Long-term semantic memory" in response.text
    assert t.vector_recalls == [("chat-1", "hello", None)]
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
    assert t.heartbeat_crons == ["daily_summary", "health_check", "notification_digest"]
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
    assert t.heartbeat_crons == ["vector_memory_consolidation"]


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
