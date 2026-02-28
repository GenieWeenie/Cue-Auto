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
):
    t = SimpleNamespace(
        action_registry_bots=[],
        memory_turns=[],
        skill_loader_unloads=[],
        skill_loader_reloads=[],
        action_unloads=[],
        action_reloads=[],
        approval_callbacks=[],
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
            self.high_risk_tools = ["run_shell"]
            self.skills_dir = "skills"
            self.skills_hot_reload = skills_hot_reload
            self.heartbeat_enabled = heartbeat_enabled
            self.daily_summary_cron = "0 8 * * *"
            self.telegram_admin_chat_id = 42
            self.loop_enabled = loop_enabled
            self.loop_interval_seconds = 1
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

    class FakeStateManager:
        def __init__(self, db_path):  # noqa: ARG002
            pass

    class FakeSoulLoader:
        def __init__(self, path):  # noqa: ARG002
            pass

        def inject(self, text: str) -> str:
            return text

    class FakeRouter:
        def __init__(self, config):  # noqa: ARG002
            pass

        def health_check(self):
            return {"openai": True}

        def health_status(self):
            return {"openai": "unknown"}

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
        def __init__(self, tools):  # noqa: ARG002
            pass

    class FakeApprovalGate:
        def __init__(self, classifier, approval_gateway=None, tool_name_lookup=None):  # noqa: ARG002
            pass

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
    monkeypatch.setattr(app_module, "ActionRegistry", FakeActionRegistry)
    monkeypatch.setattr(app_module, "AsyncLocalExecutor", FakeExecutor)
    monkeypatch.setattr(app_module, "RiskClassifier", FakeRiskClassifier)
    monkeypatch.setattr(app_module, "ApprovalGate", FakeApprovalGate)
    monkeypatch.setattr(app_module, "Heartbeat", FakeHeartbeat)
    monkeypatch.setattr(app_module, "TelegramGateway", FakeTelegramGateway)
    monkeypatch.setattr(app_module, "ApprovalGateway", FakeApprovalGateway)
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
    assert t.action_registry_bots == [None]
    health = app._build_health_status()
    assert health["status"] == "ok"
    assert health["providers"] == {"openai": "unknown"}


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
    assert t.heartbeat_crons == ["daily_summary", "health_check"]
    assert t.loop_run_once == 1
    assert t.watcher_stop == 1
    assert t.loop_stop == 1
    assert t.heartbeat_stop == 1
    assert t.telegram_stop == 1


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
    _install_fakes(monkeypatch, has_telegram=True, outage_on_chat=True)
    app = app_module.CueApp()
    msg = UnifiedMessage(platform="telegram", chat_id="chat-1", user_id="u1", text="hello")

    response = await app._handle_message(msg)
    assert "temporarily unavailable" in response.text
    assert len(app._queued_messages) == 1

    # Outage notification sent to admin chat once.
    assert len(app.telegram.app.bot.sent) == 1
    assert app.telegram.app.bot.sent[0]["chat_id"] == app.config.telegram_admin_chat_id

    # Second message should queue but avoid duplicate outage spam.
    _ = await app._handle_message(msg)
    assert len(app._queued_messages) == 2
    assert len(app.telegram.app.bot.sent) == 1
