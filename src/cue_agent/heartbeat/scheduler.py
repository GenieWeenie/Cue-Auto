"""APScheduler-based heartbeat for timed autonomous tasks."""

from __future__ import annotations

import logging
from typing import Callable, Coroutine

from apscheduler import AsyncScheduler
from apscheduler.triggers.cron import CronTrigger

from cue_agent.config import CueConfig

logger = logging.getLogger(__name__)


class Heartbeat:
    """Wraps APScheduler to manage cron-based scheduled tasks."""

    def __init__(self, config: CueConfig):
        self.config = config
        self._scheduler: AsyncScheduler | None = None
        self._started = False

    async def start(self) -> None:
        if not self.config.heartbeat_enabled:
            logger.info("Heartbeat disabled")
            return

        self._scheduler = AsyncScheduler()
        await self._scheduler.__aenter__()
        logger.info("Heartbeat started")
        self._started = True

    async def add_cron_task(
        self,
        task_id: str,
        func: Callable[..., Coroutine],
        cron_expr: str,
    ) -> None:
        """Schedule an async function on a cron expression."""
        if self._scheduler is None:
            logger.warning("Heartbeat not started — cannot schedule %s", task_id)
            return

        trigger = CronTrigger.from_crontab(cron_expr)
        await self._scheduler.add_schedule(func, trigger, id=task_id)
        logger.info("Scheduled task %s: %s", task_id, cron_expr)

    async def stop(self) -> None:
        if self._started and self._scheduler is not None:
            await self._scheduler.__aexit__(None, None, None)
            self._started = False
            logger.info("Heartbeat stopped")
