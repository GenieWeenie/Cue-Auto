"""Built-in scheduled tasks for the heartbeat."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, cast

logger = logging.getLogger(__name__)


async def daily_summary(
    brain: Any,
    memory: Any,
    bot: Any,
    admin_chat_id: int,
    task_queue: Any | None = None,
    router: Any | None = None,
    notifier: Any | None = None,
) -> None:
    """Generate and send a daily activity summary to the admin."""
    logger.info("Running daily summary task")

    context = memory.get_context("system_loop", limit=50)
    if not context:
        summary = "No activity recorded in the last period."
    else:
        summary = brain.chat(f"Summarize the following agent activity in 3-5 bullet points:\n\n{context}")

    task_stats = task_queue.queue_stats() if task_queue is not None else {}
    provider_health = router.health_status() if router is not None else {}
    usage = router.usage_summary() if router is not None else {}
    event_counts = notifier.event_counters() if notifier is not None else {}
    recent_errors = notifier.recent_errors(limit=5) if notifier is not None else []

    task_line = ", ".join(f"{k}={v}" for k, v in task_stats.items()) if task_stats else "n/a"
    health_line = ", ".join(f"{k}:{v}" for k, v in provider_health.items()) if provider_health else "n/a"
    usage_total = float(usage.get("total_estimated_cost_usd", 0.0)) if usage else 0.0
    usage_month = str(usage.get("month", "n/a")) if usage else "n/a"
    events_line = ", ".join(f"{k}={v}" for k, v in sorted(event_counts.items())) if event_counts else "n/a"
    error_block = "\n".join(f"- {line}" for line in recent_errors) if recent_errors else "- none"

    await bot.send_message(
        chat_id=admin_chat_id,
        text=(
            f"**Daily Summary** — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
            f"{summary}\n\n"
            f"**Task Queue**\n- {task_line}\n\n"
            f"**Tools & Alerts**\n- {events_line}\n\n"
            f"**Costs**\n- Month: `{usage_month}`\n- Estimated spend: `${usage_total:.4f}`\n\n"
            f"**Provider Health**\n- {health_line}\n\n"
            f"**Recent Errors**\n{error_block}"
        ),
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


async def consolidate_vector_memory(
    brain: Any,
    vector_memory: Any,
    *,
    min_entries: int,
    keep_recent: int,
    max_items: int,
) -> None:
    """Summarize and compact old vector-memory entries."""
    if not getattr(vector_memory, "is_available", False):
        logger.info("Skipping vector memory consolidation; backend unavailable")
        return

    logger.info(
        "Running vector memory consolidation",
        extra={
            "event": "vector_memory_consolidation_started",
            "min_entries": min_entries,
            "keep_recent": keep_recent,
            "max_items": max_items,
        },
    )

    def _summarizer(chat_id: str, snippets: list[str]) -> str:
        excerpt = "\n".join(f"- {snippet}" for snippet in snippets[:50])
        prompt = (
            "Summarize these prior memories into compact, durable facts for future recall. "
            "Return up to 8 concise bullet points.\n\n"
            f"Chat ID: {chat_id}\n"
            f"Memories:\n{excerpt}"
        )
        return cast(str, brain.chat(prompt))

    try:
        summary = vector_memory.consolidate_all(
            summarizer=_summarizer,
            min_entries=min_entries,
            keep_recent=keep_recent,
            max_items=max_items,
        )
    except Exception:
        logger.exception(
            "Vector memory consolidation failed",
            extra={"event": "vector_memory_consolidation_failed"},
        )
        return

    logger.info(
        "Vector memory consolidation complete",
        extra={
            "event": "vector_memory_consolidation_complete",
            **summary,
        },
    )


async def cleanup_audit_trail(
    audit_trail: Any,
    *,
    retention_days: int,
) -> None:
    """Delete audit rows older than configured retention."""
    if retention_days <= 0:
        logger.info("Skipping audit cleanup; retention disabled")
        return

    deleted = audit_trail.cleanup_older_than(retention_days)
    logger.info(
        "Audit retention cleanup complete",
        extra={
            "event": "audit_retention_cleanup_complete",
            "retention_days": retention_days,
            "deleted_rows": deleted,
        },
    )
