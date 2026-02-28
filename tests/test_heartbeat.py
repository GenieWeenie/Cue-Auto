"""Tests for heartbeat scheduler and built-in tasks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cue_agent.config import CueConfig
from cue_agent.heartbeat.scheduler import Heartbeat
from cue_agent.heartbeat.tasks import cleanup_audit_trail, consolidate_vector_memory, daily_summary, health_check


class _FakeScheduler:
    def __init__(self):
        self.entered = False
        self.exited = False
        self.schedules: list[dict] = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        del exc_type, exc, tb
        self.exited = True

    async def add_schedule(self, func, trigger, id: str):
        self.schedules.append({"func": func, "trigger": trigger, "id": id})


@pytest.mark.asyncio
async def test_heartbeat_start_add_stop(monkeypatch):
    fake_scheduler = _FakeScheduler()
    monkeypatch.setattr("cue_agent.heartbeat.scheduler.AsyncScheduler", lambda: fake_scheduler)
    monkeypatch.setattr("cue_agent.heartbeat.scheduler.CronTrigger.from_crontab", lambda expr: f"trigger:{expr}")

    hb = Heartbeat(CueConfig(heartbeat_enabled=True))
    await hb.start()
    assert fake_scheduler.entered is True

    async def _job():
        return None

    await hb.add_cron_task("job-1", _job, "*/5 * * * *")
    assert fake_scheduler.schedules == [{"func": _job, "trigger": "trigger:*/5 * * * *", "id": "job-1"}]

    await hb.stop()
    assert fake_scheduler.exited is True


@pytest.mark.asyncio
async def test_heartbeat_disabled_does_not_start():
    hb = Heartbeat(CueConfig(heartbeat_enabled=False))
    await hb.start()
    assert hb._scheduler is None


@pytest.mark.asyncio
async def test_add_cron_task_without_start_is_noop():
    hb = Heartbeat(CueConfig(heartbeat_enabled=True))

    async def _job():
        return None

    await hb.add_cron_task("job-1", _job, "* * * * *")
    assert hb._scheduler is None


@pytest.mark.asyncio
async def test_daily_summary_without_context():
    class _FakeBrain:
        def chat(self, prompt: str) -> str:
            return f"summary:{prompt}"

    class _FakeMemory:
        def get_context(self, chat_id: str, limit: int):  # noqa: ARG002
            assert chat_id == "system_loop"
            return ""

    class _FakeBot:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = _FakeBot()
    await daily_summary(_FakeBrain(), _FakeMemory(), bot, admin_chat_id=123)

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 123
    assert "No activity recorded" in bot.sent[0]["text"]
    assert bot.sent[0]["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_daily_summary_with_context_calls_brain():
    class _FakeBrain:
        def __init__(self):
            self.prompts: list[str] = []

        def chat(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "bullet 1\nbullet 2"

    class _FakeMemory:
        def get_context(self, chat_id: str, limit: int):  # noqa: ARG002
            return "task A completed"

    class _FakeBot:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    brain = _FakeBrain()
    bot = _FakeBot()
    await daily_summary(brain, _FakeMemory(), bot, admin_chat_id=55)

    assert len(brain.prompts) == 1
    assert "Summarize the following agent activity" in brain.prompts[0]
    assert "task A completed" in brain.prompts[0]
    assert "bullet 1" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_daily_summary_includes_operational_sections():
    class _FakeBrain:
        def chat(self, prompt: str) -> str:  # noqa: ARG002
            return "ops summary"

    class _FakeMemory:
        def get_context(self, chat_id: str, limit: int):  # noqa: ARG002
            return "activity"

    class _FakeBot:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    queue = SimpleNamespace(queue_stats=lambda: {"pending": 1, "failed": 2})
    router = SimpleNamespace(
        health_status=lambda: {"openai": "up"},
        usage_summary=lambda: {"month": "2026-02", "total_estimated_cost_usd": 12.34},
    )
    notifier = SimpleNamespace(
        event_counters=lambda: {"task_completion": 3, "budget_warning": 1},
        recent_errors=lambda limit=5: ["provider timeout"],  # noqa: ARG005
    )
    bot = _FakeBot()
    await daily_summary(
        _FakeBrain(),
        _FakeMemory(),
        bot,
        admin_chat_id=1,
        task_queue=queue,
        router=router,
        notifier=notifier,
    )
    text = bot.sent[0]["text"]
    assert "**Task Queue**" in text
    assert "**Tools & Alerts**" in text
    assert "**Costs**" in text
    assert "**Provider Health**" in text
    assert "**Recent Errors**" in text


@pytest.mark.asyncio
async def test_health_check_logs_provider_states(monkeypatch):
    records: list[tuple[int, str, str]] = []

    def _capture(level: int, message: str, provider: str, state: str):
        records.append((level, provider, state))

    monkeypatch.setattr("cue_agent.heartbeat.tasks.logger.log", _capture)
    brain = SimpleNamespace(router=SimpleNamespace(health_check=lambda: {"openai": True, "anthropic": False}))

    await health_check(brain)
    assert records == [
        (20, "openai", "OK"),
        (30, "anthropic", "UNREACHABLE"),
    ]


@pytest.mark.asyncio
async def test_vector_memory_consolidation_noop_when_unavailable():
    class _FakeBrain:
        def chat(self, prompt: str) -> str:  # noqa: ARG002
            return "summary"

    class _FakeVectorMemory:
        is_available = False

        def consolidate_all(self, **kwargs):  # noqa: ARG002
            raise AssertionError("should not be called")

    await consolidate_vector_memory(
        _FakeBrain(),
        _FakeVectorMemory(),
        min_entries=10,
        keep_recent=5,
        max_items=50,
    )


@pytest.mark.asyncio
async def test_vector_memory_consolidation_invokes_summarizer():
    class _FakeBrain:
        def __init__(self):
            self.prompts: list[str] = []

        def chat(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "summary bullets"

    class _FakeVectorMemory:
        is_available = True

        def __init__(self):
            self.called_with: dict = {}

        def consolidate_all(self, **kwargs):
            self.called_with = kwargs
            summary = kwargs["summarizer"]("chat-1", ["alpha", "beta"])
            assert "summary bullets" == summary
            return {"consolidated_chats": 1, "deleted_entries": 2}

    brain = _FakeBrain()
    vm = _FakeVectorMemory()
    await consolidate_vector_memory(
        brain,
        vm,
        min_entries=10,
        keep_recent=5,
        max_items=50,
    )

    assert vm.called_with["min_entries"] == 10
    assert vm.called_with["keep_recent"] == 5
    assert vm.called_with["max_items"] == 50
    assert len(brain.prompts) == 1
    assert "Memories:" in brain.prompts[0]


@pytest.mark.asyncio
async def test_cleanup_audit_trail_deletes_old_rows():
    class _FakeAuditTrail:
        def __init__(self):
            self.days: list[int] = []

        def cleanup_older_than(self, days: int) -> int:
            self.days.append(days)
            return 7

    audit = _FakeAuditTrail()
    await cleanup_audit_trail(audit, retention_days=30)
    assert audit.days == [30]


@pytest.mark.asyncio
async def test_cleanup_audit_trail_skips_when_disabled():
    class _FakeAuditTrail:
        def cleanup_older_than(self, days: int) -> int:  # noqa: ARG002
            raise AssertionError("should not run")

    await cleanup_audit_trail(_FakeAuditTrail(), retention_days=0)
