"""Tests for heartbeat scheduler and built-in tasks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cue_agent.config import CueConfig
from cue_agent.heartbeat.scheduler import Heartbeat
from cue_agent.heartbeat.tasks import daily_summary, health_check


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
