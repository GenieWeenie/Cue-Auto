"""Tests for notification batching, quiet-hours, and priority filtering."""

from __future__ import annotations

from datetime import datetime

import pytest

from cue_agent.config import CueConfig
from cue_agent.notifications.manager import NotificationManager


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):  # noqa: ANN003
        self.sent.append(kwargs)


def _make_now(hour: int):
    return lambda: datetime(2026, 2, 28, hour, 0, 0)


@pytest.mark.asyncio
async def test_immediate_mode_sends_event():
    bot = _FakeBot()
    manager = NotificationManager(
        CueConfig(
            notifications_enabled=True,
            notification_delivery_mode="immediate",
            notification_priority_threshold="low",
            notification_quiet_hours_start=22,
            notification_quiet_hours_end=7,
            notification_timezone="UTC",
        ),
        bot=bot,
        admin_chat_id=42,
        now_provider=_make_now(12),
    )
    manager.emit(
        category="task_completion",
        priority="high",
        title="Task complete",
        body="Task #1 succeeded",
    )

    sent = await manager.flush(batched=False)
    assert sent == 1
    assert len(bot.sent) == 1
    assert "Task complete" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_quiet_hours_defers_non_critical():
    bot = _FakeBot()
    manager = NotificationManager(
        CueConfig(
            notifications_enabled=True,
            notification_delivery_mode="immediate",
            notification_priority_threshold="low",
            notification_quiet_hours_start=22,
            notification_quiet_hours_end=7,
            notification_timezone="UTC",
        ),
        bot=bot,
        admin_chat_id=42,
        now_provider=_make_now(23),
    )
    manager.emit(
        category="task_completion",
        priority="high",
        title="Task failed",
        body="Task #1 failed",
    )
    sent = await manager.flush(batched=False)
    assert sent == 0
    assert manager.queue_size() == 1
    assert bot.sent == []


@pytest.mark.asyncio
async def test_hourly_mode_batches_digest():
    bot = _FakeBot()
    manager = NotificationManager(
        CueConfig(
            notifications_enabled=True,
            notification_delivery_mode="hourly",
            notification_priority_threshold="medium",
            notification_quiet_hours_start=22,
            notification_quiet_hours_end=7,
            notification_timezone="UTC",
        ),
        bot=bot,
        admin_chat_id=42,
        now_provider=_make_now(12),
    )
    manager.emit(category="task_completion", priority="high", title="Task done", body="Task #2 complete")
    manager.emit(category="budget_warning", priority="critical", title="Budget", body="Hard stop")

    sent = await manager.flush(batched=True)
    assert sent == 2
    assert len(bot.sent) == 1
    assert "Notification Digest" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_priority_threshold_filters_low_events():
    bot = _FakeBot()
    manager = NotificationManager(
        CueConfig(
            notifications_enabled=True,
            notification_delivery_mode="immediate",
            notification_priority_threshold="high",
            notification_quiet_hours_start=22,
            notification_quiet_hours_end=7,
            notification_timezone="UTC",
        ),
        bot=bot,
        admin_chat_id=42,
        now_provider=_make_now(12),
    )
    manager.emit(category="task_completion", priority="medium", title="Ignored", body="No send")
    manager.emit(category="outage", priority="critical", title="Outage", body="Provider down")

    sent = await manager.flush(batched=False)
    assert sent == 1
    assert len(bot.sent) == 1
    assert "Outage" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_disabled_category_not_queued_nor_sent():
    """Events in notification_categories_disabled are not queued and send_message is not called."""
    bot = _FakeBot()
    manager = NotificationManager(
        CueConfig(
            notifications_enabled=True,
            notification_delivery_mode="immediate",
            notification_priority_threshold="low",
            notification_quiet_hours_start=22,
            notification_quiet_hours_end=7,
            notification_timezone="UTC",
            notification_categories_disabled="task_completion",
        ),
        bot=bot,
        admin_chat_id=42,
        now_provider=_make_now(12),
    )
    manager.emit(
        category="task_completion",
        priority="high",
        title="Task complete",
        body="Should not send",
    )
    assert manager.queue_size() == 0
    sent = await manager.flush(batched=False)
    assert sent == 0
    assert len(bot.sent) == 0


@pytest.mark.asyncio
async def test_disabled_category_other_category_still_delivered():
    """Events in a non-disabled category are still queued and delivered."""
    bot = _FakeBot()
    manager = NotificationManager(
        CueConfig(
            notifications_enabled=True,
            notification_delivery_mode="immediate",
            notification_priority_threshold="low",
            notification_quiet_hours_start=22,
            notification_quiet_hours_end=7,
            notification_timezone="UTC",
            notification_categories_disabled="task_completion",
        ),
        bot=bot,
        admin_chat_id=42,
        now_provider=_make_now(12),
    )
    manager.emit(
        category="approval",
        priority="high",
        title="Approval needed",
        body="Please approve",
    )
    assert manager.queue_size() == 1
    sent = await manager.flush(batched=False)
    assert sent == 1
    assert len(bot.sent) == 1
    assert "Approval needed" in bot.sent[0]["text"]
