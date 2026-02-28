"""Application orchestrator — wires all 6 blocks together."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from environment.executor import AsyncLocalExecutor
from protocol.state_manager import StateManager

from cue_agent.actions.registry import ActionRegistry
from cue_agent.brain.cue_brain import CueBrain
from cue_agent.brain.llm_router import LLMAllProvidersDownError, LLMRouter
from cue_agent.brain.soul_loader import SoulLoader
from cue_agent.comms.approval_gateway import ApprovalGateway
from cue_agent.comms.models import UnifiedMessage, UnifiedResponse
from cue_agent.comms.telegram_gateway import TelegramGateway
from cue_agent.config import CueConfig
from cue_agent.heartbeat.scheduler import Heartbeat
from cue_agent.heartbeat.tasks import consolidate_vector_memory, daily_summary, health_check
from cue_agent.health.server import HealthServer
from cue_agent.loop.ralph_loop import RalphLoop
from cue_agent.loop.task_queue import TaskQueue
from cue_agent.logging_utils import correlation_context, get_correlation_id, new_correlation_id, setup_logging
from cue_agent.memory.session_memory import SessionMemory
from cue_agent.memory.vector_memory import VectorMemory
from cue_agent.notifications.manager import NotificationManager
from cue_agent.security.approval_gate import ApprovalGate
from cue_agent.security.risk_classifier import RiskClassifier
from cue_agent.skills.loader import SkillLoader
from cue_agent.skills.watcher import SkillWatcher

logger = logging.getLogger(__name__)


class CueApp:
    def __init__(self) -> None:
        self.config = CueConfig()
        self._setup_logging()
        self.notification_manager: NotificationManager | None = None

        # --- Memory (EAP StateManager) ---
        self.state_manager = StateManager(db_path=self.config.state_db_path)
        self.task_queue = TaskQueue(db_path=self.config.state_db_path)

        # --- Brain ---
        self.soul_loader = SoulLoader(self.config.soul_md_path)
        self.router = LLMRouter(self.config, event_handler=self._handle_router_event)
        self.brain = CueBrain(self.config, self.soul_loader, self.router)

        # --- Memory ---
        self.memory = SessionMemory(self.state_manager)
        self.vector_memory = VectorMemory(self.config)

        # --- Actions ---
        self.actions = ActionRegistry()
        self.executor = AsyncLocalExecutor(self.state_manager, self.actions.eap_registry)

        # --- Security ---
        self.risk_classifier = RiskClassifier(
            self.config.high_risk_tools,
            approval_required_levels=self.config.approval_required_levels,
            rules_path=self.config.risk_rules_path,
            sandbox_dry_run=self.config.risk_sandbox_dry_run,
        )
        self.approval_gateway: ApprovalGateway | None = None
        self.approval_gate = ApprovalGate(self.risk_classifier, risk_event_handler=self._handle_risk_event)

        # --- Heartbeat ---
        self.heartbeat = Heartbeat(self.config)

        # --- Comms (Telegram) ---
        self.telegram: TelegramGateway | None = None
        if self.config.has_telegram:
            self.telegram = TelegramGateway(
                config=self.config,
                on_message=self._handle_message,
                on_approval=self._handle_approval,
            )
            # Wire Telegram bot into actions and approval
            bot = self.telegram.app.bot
            self.actions = ActionRegistry(telegram_bot=bot)
            self.executor = AsyncLocalExecutor(self.state_manager, self.actions.eap_registry)
            self.approval_gateway = ApprovalGateway(bot, self.config.telegram_admin_chat_id)
            self.notification_manager = NotificationManager(
                self.config,
                bot=bot,
                admin_chat_id=self.config.telegram_admin_chat_id,
            )
            self.approval_gate = ApprovalGate(
                self.risk_classifier,
                approval_gateway=self.approval_gateway,
                tool_name_lookup=self.actions.get_hashed_manifest(),
                risk_event_handler=self._handle_risk_event,
            )

        # --- Skills ---
        self.skill_loader = SkillLoader(self.config.skills_dir)
        loaded_skills = self.skill_loader.load_all()
        if loaded_skills:
            self.actions.load_skills(loaded_skills)
            logger.info("Loaded %d skills: %s", len(loaded_skills), list(loaded_skills.keys()))

        self.skill_watcher = SkillWatcher(
            self.config.skills_dir,
            on_change=self._handle_skill_change,
        )

        # --- Rebuild executor after skills loaded ---
        self.executor = AsyncLocalExecutor(self.state_manager, self.actions.eap_registry)

        # --- Ralph Loop ---
        self.ralph_loop = RalphLoop(
            brain=self.brain,
            memory=self.memory,
            vector_memory=self.vector_memory,
            task_queue=self.task_queue,
            actions=self.actions,
            executor=self.executor,
            state_manager=self.state_manager,
            approval_gate=self.approval_gate,
            config=self.config,
            notification_handler=self._handle_loop_event,
        )
        self._queued_messages: list[dict[str, Any]] = []
        self._provider_outage_notified = False
        self.health_server = HealthServer(
            host=self.config.healthcheck_host,
            port=self.config.healthcheck_port,
            status_provider=self._build_health_status,
        )

    def _setup_logging(self) -> None:
        setup_logging()

    async def _handle_message(self, msg: UnifiedMessage) -> UnifiedResponse:
        """Process an incoming message through the brain."""
        if not get_correlation_id():
            with correlation_context(new_correlation_id("tg")):
                return await self._handle_message(msg)

        command_response = self._handle_task_commands(msg)
        if command_response is not None:
            return command_response

        self.memory.add_turn(msg.chat_id, "user", msg.text)
        self.vector_memory.add_turn(msg.chat_id, "user", msg.text)
        context = self.memory.get_context(msg.chat_id)
        vector_context = self.vector_memory.recall_as_context(msg.chat_id, msg.text)
        if vector_context:
            context = f"{context}\n\n{vector_context}" if context else vector_context
        try:
            response_text = self.brain.chat(msg.text, extra_context=context)
            self._provider_outage_notified = False
        except LLMAllProvidersDownError as exc:
            self._queued_messages.append(
                {
                    "chat_id": msg.chat_id,
                    "user_id": msg.user_id,
                    "username": msg.username,
                    "text": msg.text,
                }
            )
            logger.warning(
                "Queued message due to provider outage",
                extra={
                    "event": "message_queued_provider_outage",
                    "queue_size": len(self._queued_messages),
                    "provider_status": exc.provider_status,
                },
            )
            if self.telegram and not self._provider_outage_notified:
                await self._notify_provider_outage(exc.provider_status)
                self._provider_outage_notified = True
            response_text = (
                "All LLM providers are temporarily unavailable. "
                "Your message has been queued and the admin has been notified."
            )

        self.memory.add_turn(msg.chat_id, "assistant", response_text)
        self.vector_memory.add_turn(msg.chat_id, "assistant", response_text)
        return UnifiedResponse(text=response_text, chat_id=msg.chat_id)

    def _handle_task_commands(self, msg: UnifiedMessage) -> UnifiedResponse | None:
        text = msg.text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split()
        command = parts[0].lower()

        try:
            if command == "/tasks":
                status = parts[1].lower() if len(parts) > 1 else None
                if status in {"all", "*"}:
                    status = None
                tasks = self.task_queue.list_tasks(status=status, limit=self.config.task_queue_max_list)
                return UnifiedResponse(
                    text=self._format_task_list(tasks),
                    chat_id=msg.chat_id,
                )

            if command == "/usage":
                return UnifiedResponse(
                    text=self.router.usage_report_text(),
                    chat_id=msg.chat_id,
                )

            if command != "/task":
                return None

            if len(parts) < 2:
                return UnifiedResponse(text=self._task_help_text(), chat_id=msg.chat_id)

            subcommand = parts[1].lower()
            if subcommand == "add":
                if len(parts) < 3:
                    return UnifiedResponse(text="Usage: /task add [p1|p2|p3|p4] <title>", chat_id=msg.chat_id)
                priority = 3
                title_start = 2
                if parts[2].lower().startswith("p") and parts[2][1:].isdigit():
                    priority = int(parts[2][1:])
                    title_start = 3
                title = " ".join(parts[title_start:]).strip()
                task_id = self.task_queue.create_task(
                    title=title,
                    priority=priority,
                    source="telegram",
                )
                return UnifiedResponse(text=f"Created task #{task_id}: {title}", chat_id=msg.chat_id)

            if subcommand == "sub":
                if len(parts) < 4:
                    return UnifiedResponse(
                        text="Usage: /task sub <parent_id> [p1|p2|p3|p4] <title>", chat_id=msg.chat_id
                    )
                parent_id = int(parts[2])
                priority = 3
                title_start = 3
                if parts[3].lower().startswith("p") and parts[3][1:].isdigit():
                    priority = int(parts[3][1:])
                    title_start = 4
                title = " ".join(parts[title_start:]).strip()
                task_id = self.task_queue.create_subtask(
                    parent_task_id=parent_id,
                    title=title,
                    priority=priority,
                    source="telegram_subtask",
                )
                return UnifiedResponse(text=f"Created sub-task #{task_id} under #{parent_id}", chat_id=msg.chat_id)

            if subcommand == "done":
                if len(parts) != 3:
                    return UnifiedResponse(text="Usage: /task done <task_id>", chat_id=msg.chat_id)
                task_id = int(parts[2])
                self.task_queue.mark_done(task_id)
                return UnifiedResponse(text=f"Marked task #{task_id} as done", chat_id=msg.chat_id)

            if subcommand == "depend":
                if len(parts) != 4:
                    return UnifiedResponse(
                        text="Usage: /task depend <task_id> <depends_on_task_id>", chat_id=msg.chat_id
                    )
                task_id = int(parts[2])
                dep_id = int(parts[3])
                self.task_queue.add_dependency(task_id, dep_id)
                return UnifiedResponse(
                    text=f"Task #{task_id} now depends on task #{dep_id}",
                    chat_id=msg.chat_id,
                )

            if subcommand == "retry":
                if len(parts) != 3:
                    return UnifiedResponse(text="Usage: /task retry <task_id>", chat_id=msg.chat_id)
                task_id = int(parts[2])
                self.task_queue.retry_task(task_id)
                return UnifiedResponse(text=f"Retried task #{task_id}", chat_id=msg.chat_id)

            return UnifiedResponse(text=self._task_help_text(), chat_id=msg.chat_id)
        except ValueError as exc:
            return UnifiedResponse(text=f"Task command error: {exc}", chat_id=msg.chat_id)

    def _format_task_list(self, tasks: list[dict[str, Any]]) -> str:
        if not tasks:
            return "No tasks in queue."

        lines = ["Task Queue:"]
        for task in tasks:
            task_id = task["id"]
            status = task["status"]
            priority = task["priority"]
            title = task["title"]
            deps = task["depends_on"]
            suffix = f" deps={deps}" if deps else ""
            lines.append(f"- #{task_id} [{status}] p{priority} {title}{suffix}")
        return "\n".join(lines)

    def _task_help_text(self) -> str:
        return (
            "Task commands:\n"
            "- /usage\n"
            "- /tasks [status|all]\n"
            "- /task add [p1|p2|p3|p4] <title>\n"
            "- /task sub <parent_id> [p1|p2|p3|p4] <title>\n"
            "- /task done <task_id>\n"
            "- /task depend <task_id> <depends_on_task_id>\n"
            "- /task retry <task_id>"
        )

    def _notify_event(self, *, category: str, priority: str, title: str, body: str, metadata: dict[str, Any]) -> None:
        if self.notification_manager is None:
            return
        self.notification_manager.emit(
            category=category,
            priority=priority,
            title=title,
            body=body,
            metadata=metadata,
        )

    def _handle_router_event(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event", ""))
        if event_name == "llm_budget_warning":
            spend = float(event.get("monthly_spend_usd", 0.0))
            warn = float(event.get("warning_threshold_usd", 0.0))
            self._notify_event(
                category="budget_warning",
                priority="high",
                title="LLM monthly budget warning",
                body=f"Estimated spend ${spend:.2f} exceeded warning ${warn:.2f}",
                metadata=event,
            )
        elif event_name == "llm_budget_hard_stop":
            spend = float(event.get("monthly_spend_usd", 0.0))
            hard = float(event.get("hard_stop_threshold_usd", 0.0))
            self._notify_event(
                category="budget_warning",
                priority="critical",
                title="LLM monthly budget hard stop",
                body=f"Estimated spend ${spend:.2f} exceeded hard-stop ${hard:.2f}",
                metadata=event,
            )

    def _handle_risk_event(self, event: dict[str, Any]) -> None:
        if str(event.get("event", "")) != "high_risk_action":
            return
        risk_level = str(event.get("risk_level", "high")).lower()
        priority = "critical" if risk_level == "critical" else "high"
        tool_name = str(event.get("tool_name", "unknown"))
        reason = str(event.get("reason", "approval required"))
        self._notify_event(
            category="high_risk_action",
            priority=priority,
            title=f"Approval required: {tool_name}",
            body=reason,
            metadata=event,
        )

    def _handle_loop_event(self, event: dict[str, Any]) -> None:
        category = str(event.get("event", "task_completion"))
        priority = str(event.get("priority", "medium"))
        title = str(event.get("title", "Loop event"))
        body = str(event.get("body", ""))
        self._notify_event(
            category=category,
            priority=priority,
            title=title,
            body=body,
            metadata=event,
        )

    async def _notify_provider_outage(self, provider_status: dict[str, str]) -> None:
        if self.telegram is None:
            return

        summary = ", ".join(f"{name}={status}" for name, status in provider_status.items())
        self._notify_event(
            category="outage",
            priority="critical",
            title="LLM provider outage",
            body=f"Incoming messages are queued. Provider status: {summary}",
            metadata={"provider_status": provider_status},
        )
        if self.notification_manager is not None:
            await self.notification_manager.flush(force=True, batched=False)

    async def _handle_approval(self, approval_id: str, approved: bool) -> None:
        """Route Telegram approval callbacks to the approval gateway."""
        if self.approval_gateway:
            await self.approval_gateway.handle_callback(approval_id, approved)

    async def _handle_skill_change(self, path: Path, event_type: str) -> None:
        """Handle skill file changes for hot-reload."""
        if event_type == "deleted":
            # Determine skill name from path
            name = path.stem if path.is_file() or not path.exists() else path.name
            self.skill_loader.unload_skill(name)
            self.actions.unload_skill(name)
            logger.info("Unloaded deleted skill: %s", name)
        else:
            # Created or modified — (re)load
            try:
                skill = self.skill_loader.reload_skill(path)
                self.actions.reload_skill(skill)
                logger.info("Hot-reloaded skill: %s (%d tools)", skill.name, len(skill.tools))
            except Exception:
                logger.exception("Failed to hot-reload skill from %s", path)

    async def start(self, mode: str = "polling") -> None:
        """Start CueAgent in the specified mode."""
        logger.info("Starting CueAgent in '%s' mode", mode)
        logger.info(
            "Tools: %d total (%d skills: %s)",
            self.actions.tool_count,
            len(self.actions.skill_names),
            self.actions.skill_names,
        )

        if self.config.healthcheck_enabled:
            await self.health_server.start()

        # Start heartbeat
        await self.heartbeat.start()
        if self.config.heartbeat_enabled and self.telegram:
            bot = self.telegram.app.bot
            await self.heartbeat.add_cron_task(
                "daily_summary",
                partial(
                    daily_summary,
                    brain=self.brain,
                    memory=self.memory,
                    bot=bot,
                    admin_chat_id=self.config.telegram_admin_chat_id,
                    task_queue=self.task_queue,
                    router=self.router,
                    notifier=self.notification_manager,
                ),
                self.config.daily_summary_cron,
            )
            await self.heartbeat.add_cron_task(
                "health_check",
                partial(health_check, brain=self.brain),
                "*/30 * * * *",
            )

        if self.config.heartbeat_enabled and self.notification_manager:
            digest_mode = self.config.notification_delivery_mode.strip().lower()
            digest_cron = self.config.notification_hourly_digest_cron
            if digest_mode == "daily":
                digest_cron = self.config.notification_daily_digest_cron
            await self.heartbeat.add_cron_task(
                "notification_digest",
                partial(
                    self._flush_notifications_digest,
                    batched=(digest_mode != "immediate"),
                ),
                digest_cron,
            )
        if (
            self.config.heartbeat_enabled
            and self.config.vector_memory_enabled
            and self.config.vector_memory_consolidation_enabled
        ):
            await self.heartbeat.add_cron_task(
                "vector_memory_consolidation",
                partial(
                    consolidate_vector_memory,
                    brain=self.brain,
                    vector_memory=self.vector_memory,
                    min_entries=self.config.vector_memory_consolidation_min_entries,
                    keep_recent=self.config.vector_memory_consolidation_keep_recent,
                    max_items=self.config.vector_memory_consolidation_max_items,
                ),
                self.config.vector_memory_consolidation_cron,
            )

        # Start skill watcher for hot-reload
        watcher_task = None
        if self.config.skills_hot_reload:
            watcher_task = asyncio.create_task(self.skill_watcher.start())

        try:
            if mode == "polling":
                await self._run_polling()
            elif mode == "loop":
                await self.ralph_loop.run_forever()
            elif mode == "once":
                await self.ralph_loop.run_once()
            else:
                logger.error("Unknown mode: %s", mode)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            if watcher_task:
                self.skill_watcher.stop()
                watcher_task.cancel()
            await self._shutdown()

    async def _run_polling(self) -> None:
        """Run Telegram polling with optional Ralph loop."""
        if self.telegram is None:
            logger.error("Telegram not configured — set CUE_TELEGRAM_BOT_TOKEN")
            return

        await self.telegram.start_polling()

        tasks: list[asyncio.Task[Any]] = []
        if self.config.loop_enabled:
            tasks.append(asyncio.create_task(self.ralph_loop.run_forever()))

        # Keep running until interrupted
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        await stop_event.wait()

        for task in tasks:
            task.cancel()

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        self.ralph_loop.stop()
        await self.heartbeat.stop()
        if self.notification_manager is not None:
            await self.notification_manager.flush(force=True, batched=True)
        if self.config.healthcheck_enabled:
            await self.health_server.stop()
        if self.telegram:
            await self.telegram.stop()
        logger.info("CueAgent shut down")

    async def _flush_notifications_digest(self, *, batched: bool) -> None:
        if self.notification_manager is None:
            return
        await self.notification_manager.flush(batched=batched)

    def _build_health_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "providers": self.router.health_status(),
            "loop": {
                "enabled": self.config.loop_enabled,
                "running": self.ralph_loop.is_running,
                "last_iteration_time": self.ralph_loop.last_iteration_time,
            },
            "queue": {
                "queued_messages": len(self._queued_messages),
                "task_queue": self.task_queue.queue_stats(),
            },
            "notifications": {
                "enabled": self.config.notifications_enabled,
                "mode": self.config.notification_delivery_mode,
                "queued": self.notification_manager.queue_size() if self.notification_manager else 0,
            },
            "memory": {
                "vector_enabled": self.config.vector_memory_enabled,
                "vector_available": self.vector_memory.is_available,
            },
        }
