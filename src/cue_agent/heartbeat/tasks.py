"""Built-in scheduled tasks for the heartbeat."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def daily_summary(brain: Any, memory: Any, bot: Any, admin_chat_id: int) -> None:
    """Generate and send a daily activity summary to the admin."""
    logger.info("Running daily summary task")

    context = memory.get_context("system_loop", limit=50)
    if not context:
        summary = "No activity recorded in the last period."
    else:
        summary = brain.chat(f"Summarize the following agent activity in 3-5 bullet points:\n\n{context}")

    await bot.send_message(
        chat_id=admin_chat_id,
        text=f"**Daily Summary** — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n{summary}",
        parse_mode="Markdown",
    )
    logger.info("Daily summary sent to admin")


async def health_check(brain: Any) -> None:
    """Run a quick health check on the LLM providers."""
    logger.info("Running health check")
    status = brain.router.health_check()
    for provider, reachable in status.items():
        level = logging.INFO if reachable else logging.WARNING
        logger.log(level, "Provider %s: %s", provider, "OK" if reachable else "UNREACHABLE")
