"""Telegram notification manager with batching, quiet hours, and priority filtering."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cue_agent.config import CueConfig
from cue_agent.retry_utils import backoff_delay_seconds

logger = logging.getLogger(__name__)

Priority = str
_PRIORITY_VALUE: dict[Priority, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


@dataclass(frozen=True)
class NotificationEvent:
    category: str
    priority: Priority
    title: str
    body: str
    timestamp: datetime
    metadata: Mapping[str, Any]


class NotificationManager:
    def __init__(
        self,
        config: CueConfig,
        *,
        bot: Any,
        admin_chat_id: int,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self._config = config
        self._bot = bot
        self._admin_chat_id = admin_chat_id
        self._queue: list[NotificationEvent] = []
        self._event_counts: dict[str, int] = {}
        self._recent_errors: list[str] = []
        self._flush_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._now_provider = now_provider or datetime.now
        self._delivery_mode = config.notification_delivery_mode.strip().lower()
        self._priority_threshold = self._normalize_priority(config.notification_priority_threshold)
        self._emit_lock = threading.Lock()
        raw = (config.notification_categories_disabled or "").strip()
        self._disabled_categories: set[str] = {s.strip().lower() for s in raw.split(",") if s.strip()} if raw else set()
        self._quiet_start = int(config.notification_quiet_hours_start) % 24
        self._quiet_end = int(config.notification_quiet_hours_end) % 24
        self._tz = self._resolve_timezone(config.notification_timezone)
        self._pending_flush = False

    @staticmethod
    def _resolve_timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    @staticmethod
    def _normalize_priority(priority: str) -> Priority:
        lowered = priority.strip().lower()
        if lowered in _PRIORITY_VALUE:
            return lowered
        return "medium"

    @staticmethod
    def _priority_value(priority: str) -> int:
        return _PRIORITY_VALUE.get(priority.strip().lower(), _PRIORITY_VALUE["medium"])

    def _passes_threshold(self, priority: str) -> bool:
        return self._priority_value(priority) >= self._priority_value(self._priority_threshold)

    def _is_quiet_hours(self) -> bool:
        if self._quiet_start == self._quiet_end:
            return False
        now = self._now_provider().astimezone(self._tz)
        hour = now.hour
        if self._quiet_start < self._quiet_end:
            return self._quiet_start <= hour < self._quiet_end
        return hour >= self._quiet_start or hour < self._quiet_end

    def _record_error_if_needed(self, event: NotificationEvent) -> None:
        text = f"{event.title}: {event.body}"
        if event.priority in {"high", "critical"} or "error" in event.category or "outage" in event.category:
            self._recent_errors.append(text[:300])
            self._recent_errors = self._recent_errors[-20:]

    def emit(
        self,
        *,
        category: str,
        priority: str,
        title: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self._config.notifications_enabled:
            return
        if category.strip().lower() in self._disabled_categories:
            logger.debug("Notification skipped (category disabled): %s", category)
            return
        normalized_priority = self._normalize_priority(priority)
        if not self._passes_threshold(normalized_priority):
            return

        event = NotificationEvent(
            category=category,
            priority=normalized_priority,
            title=title.strip(),
            body=body.strip(),
            timestamp=self._now_provider(),
            metadata=MappingProxyType(dict(metadata or {})),
        )
        with self._emit_lock:
            self._queue.append(event)
            self._event_counts[category] = self._event_counts.get(category, 0) + 1
        self._record_error_if_needed(event)

        if self._delivery_mode == "immediate":
            self._schedule_flush(batched=False)

    def _schedule_flush(self, *, batched: bool) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running (e.g. called from a sync thread during startup).
            # Queue the flush for the next emit() that finds a running loop.
            self._pending_flush = True
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        # Drain any deferred flushes from before the loop was running
        if self._pending_flush:
            self._pending_flush = False
        self._flush_task = loop.create_task(self._flush_background(batched))

    async def _flush_background(self, batched: bool) -> None:
        await self.flush(batched=batched)

    async def flush(self, *, batched: bool | None = None, force: bool = False) -> int:
        if not self._config.notifications_enabled:
            return 0
        if batched is None:
            batched = self._delivery_mode != "immediate"

        async with self._flush_lock:
            if not self._queue:
                return 0

            sendable: list[NotificationEvent] = []
            remaining: list[NotificationEvent] = []
            quiet = self._is_quiet_hours()
            for event in self._queue:
                if not force and quiet and event.priority != "critical":
                    remaining.append(event)
                    continue
                sendable.append(event)

            if not sendable:
                self._queue = remaining
                return 0

            ok = False
            if batched:
                text = self._format_batch(sendable)
                ok = await self._send_message(text)
            else:
                ok = True
                for event in sendable:
                    if not await self._send_message(self._format_event(event)):
                        ok = False
                        break

            if ok:
                self._queue = remaining
                return len(sendable)

            # Keep failed items queued for next flush attempt.
            self._queue = sendable + remaining
            return 0

    async def _send_message(self, text: str) -> bool:
        attempts = max(1, self._config.retry_telegram_attempts)
        for attempt in range(1, attempts + 1):
            try:
                await self._bot.send_message(
                    chat_id=self._admin_chat_id,
                    text=text,
                    parse_mode="Markdown",
                )
                return True
            except Exception as exc:
                logger.warning(
                    "Notification send failed",
                    extra={
                        "event": "notification_send_failed",
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "error": str(exc),
                    },
                )
                if attempt >= attempts:
                    return False
                delay = backoff_delay_seconds(
                    attempt,
                    base_delay=self._config.retry_base_delay_seconds,
                    max_delay=self._config.retry_max_delay_seconds,
                    jitter=self._config.retry_jitter_seconds,
                )
                await asyncio.sleep(delay)
        return False

    def _format_event(self, event: NotificationEvent) -> str:
        ts = event.timestamp.astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        return (
            f"*[{event.priority.upper()}] {event.title}*\n"
            f"- Category: `{event.category}`\n"
            f"- Time: `{ts}`\n"
            f"- Details: {event.body}"
        )

    def _format_batch(self, events: list[NotificationEvent]) -> str:
        by_priority: dict[str, int] = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for event in events:
            by_priority[event.priority] = by_priority.get(event.priority, 0) + 1

        lines = [
            "*CueAgent Notification Digest*",
            (
                f"- Events: `{len(events)}` "
                f"(critical={by_priority['critical']}, high={by_priority['high']}, "
                f"medium={by_priority['medium']}, low={by_priority['low']})"
            ),
        ]
        for event in events[:20]:
            lines.append(f"- [{event.priority}] `{event.category}` {event.title}: {event.body}")
        remaining = len(events) - 20
        if remaining > 0:
            lines.append(f"- ...and {remaining} more event(s)")
        return "\n".join(lines)

    def queue_size(self) -> int:
        return len(self._queue)

    def event_counters(self) -> dict[str, int]:
        return dict(self._event_counts)

    def recent_errors(self, limit: int = 5) -> list[str]:
        return self._recent_errors[-max(1, limit) :]
