"""Application orchestrator — wires all 6 blocks together."""

from __future__ import annotations

import asyncio
import json
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
        self._started_at = datetime.now(timezone.utc)
        self._is_running = False
        self._action_timeline: list[dict[str, Any]] = []
        self._timeline_limit = max(20, self.config.dashboard_timeline_limit)

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
        self.actions = ActionRegistry(tool_event_handler=self._handle_tool_event)
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
            self.actions = ActionRegistry(telegram_bot=bot, tool_event_handler=self._handle_tool_event)
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
            dashboard_enabled=self.config.dashboard_enabled,
            dashboard_status_provider=self._build_dashboard_snapshot,
            dashboard_username=self.config.dashboard_username,
            dashboard_password=self.config.dashboard_password,
        )

    def _setup_logging(self) -> None:
        setup_logging()

    async def _handle_message(self, msg: UnifiedMessage) -> UnifiedResponse:
        """Process an incoming message through the brain."""
        if not get_correlation_id():
            with correlation_context(new_correlation_id("tg")):
                return await self._handle_message(msg)

        command_response = self._handle_commands(msg)
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

    def _handle_commands(self, msg: UnifiedMessage) -> UnifiedResponse | None:
        text = msg.text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split()
        command = parts[0].lower()

        try:
            if command == "/help":
                return UnifiedResponse(text=self._help_text(), chat_id=msg.chat_id, ui_mode="help")

            if command == "/status":
                return UnifiedResponse(text=self._status_text(), chat_id=msg.chat_id, ui_mode="status")

            if command == "/skills":
                return UnifiedResponse(text=self._skills_text(), chat_id=msg.chat_id, ui_mode="skills")

            if command == "/settings":
                return UnifiedResponse(text=self._settings_text(), chat_id=msg.chat_id, ui_mode="settings")

            if command == "/approve":
                return UnifiedResponse(text=self._approve_text(), chat_id=msg.chat_id, ui_mode="approve")

            if command == "/file":
                return UnifiedResponse(text=self._file_upload_text(msg), chat_id=msg.chat_id)

            if command == "/tasks":
                mode = parts[1].lower() if len(parts) > 1 else ""
                if mode in {"download", "export", "json"}:
                    tasks = self.task_queue.list_tasks(status=None, limit=self.config.task_queue_max_list)
                    payload = {
                        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                        "task_count": len(tasks),
                        "tasks": tasks,
                    }
                    return UnifiedResponse(
                        text=f"Exported {len(tasks)} task(s) as JSON.",
                        chat_id=msg.chat_id,
                        ui_mode="tasks",
                        document_filename="cue-agent-tasks.json",
                        document_bytes=json.dumps(payload, indent=2, ensure_ascii=True).encode("utf-8"),
                    )
                status = parts[1].lower() if len(parts) > 1 else None
                if status in {"all", "*"}:
                    status = None
                tasks = self.task_queue.list_tasks(status=status, limit=self.config.task_queue_max_list)
                return UnifiedResponse(
                    text=self._format_task_list(tasks),
                    chat_id=msg.chat_id,
                    ui_mode="tasks",
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
            "- /status\n"
            "- /skills\n"
            "- /settings\n"
            "- /approve\n"
            "- /tasks [status|all]\n"
            "- /tasks download\n"
            "- /task add [p1|p2|p3|p4] <title>\n"
            "- /task sub <parent_id> [p1|p2|p3|p4] <title>\n"
            "- /task done <task_id>\n"
            "- /task depend <task_id> <depends_on_task_id>\n"
            "- /task retry <task_id>"
        )

    def _help_text(self) -> str:
        return (
            "*CueAgent Command Center*\n\n"
            "- `/help` Show this menu\n"
            "- `/status` Runtime health and queue summary\n"
            "- `/tasks [status|all]` Task queue view\n"
            "- `/tasks download` Download queue JSON export\n"
            "- `/skills` Loaded skills and tool counts\n"
            "- `/usage` Provider usage and spend\n"
            "- `/approve` Pending approval requests\n"
            "- `/settings` Runtime settings snapshot"
        )

    def _status_text(self) -> str:
        status = self._build_health_status()
        providers = status.get("providers", {})
        loop = status.get("loop", {})
        queue = status.get("queue", {})
        notifications = status.get("notifications", {})
        memory = status.get("memory", {})

        provider_line = ", ".join(f"{k}:{v}" for k, v in providers.items()) if isinstance(providers, dict) else "n/a"
        queue_stats = queue.get("task_queue", {}) if isinstance(queue, dict) else {}
        queue_line = ", ".join(f"{k}={v}" for k, v in queue_stats.items()) if isinstance(queue_stats, dict) else "n/a"
        return (
            "*CueAgent Status*\n"
            f"- Time (UTC): `{status.get('timestamp_utc', 'n/a')}`\n"
            f"- Loop: `enabled={loop.get('enabled', False)}` `running={loop.get('running', False)}`\n"
            f"- Providers: {provider_line}\n"
            f"- Queue: {queue_line}\n"
            f"- Notifications: `enabled={notifications.get('enabled', False)}` "
            f"`mode={notifications.get('mode', 'n/a')}` `queued={notifications.get('queued', 0)}`\n"
            f"- Memory: `vector_enabled={memory.get('vector_enabled', False)}` "
            f"`vector_available={memory.get('vector_available', False)}`"
        )

    def _skills_text(self) -> str:
        lines = ["*Skills*"]
        names = self.actions.skill_names
        if not names:
            lines.append("- No skills loaded.")
        else:
            lines.append(f"- Loaded: `{len(names)}`")
            for name in names:
                lines.append(f"- `{name}`")
        lines.append(f"- Total tools: `{self.actions.tool_count}`")
        return "\n".join(lines)

    def _settings_text(self) -> str:
        return (
            "*Settings Snapshot*\n"
            f"- Loop enabled: `{self.config.loop_enabled}` interval=`{self.config.loop_interval_seconds}s`\n"
            f"- Task queue: `{self.config.task_queue_enabled}` max_list=`{self.config.task_queue_max_list}`\n"
            f"- Approval required: `{self.config.require_approval}` levels=`{self.config.approval_required_levels}`\n"
            f"- Notifications: `enabled={self.config.notifications_enabled}` "
            f"`mode={self.config.notification_delivery_mode}`\n"
            f"- Quiet hours: `{self.config.notification_quiet_hours_start}:00-{self.config.notification_quiet_hours_end}:00` "
            f"{self.config.notification_timezone}\n"
            f"- Search provider: `{self.config.search_provider}`"
        )

    def _approve_text(self) -> str:
        if self.approval_gateway is None:
            return "Approval gateway not configured."

        pending = self.approval_gateway.pending_approvals()
        if not pending:
            return "*Pending Approvals*\n- None."

        lines = ["*Pending Approvals*"]
        for row in pending[:20]:
            lines.append(f"- `{row['approval_id']}` step=`{row['step_id']}` {row['action_description'][:120]}")
        extra = len(pending) - 20
        if extra > 0:
            lines.append(f"- ...and {extra} more")
        return "\n".join(lines)

    @staticmethod
    def _file_upload_text(msg: UnifiedMessage) -> str:
        attachment = msg.raw.get("attachment")
        if not isinstance(attachment, dict):
            return "No file attachment detected."
        name = str(attachment.get("file_name", "uploaded-file"))
        kind = str(attachment.get("type", "file"))
        mime = str(attachment.get("mime_type", ""))
        return (
            "*File Received*\n"
            f"- Type: `{kind}`\n"
            f"- Name: `{name}`\n"
            f"- MIME: `{mime}`\n\n"
            "Tip: use `/tasks download` to receive a JSON export."
        )

    def _handle_tool_event(self, event: dict[str, Any]) -> None:
        tool_name = str(event.get("tool_name", "unknown"))
        arguments = event.get("arguments")
        risk_level = "unknown"
        risk_reason = ""
        if isinstance(arguments, dict):
            try:
                decision = self.risk_classifier.assess(tool_name, arguments)
                risk_level = decision.level
                risk_reason = decision.reason
            except Exception:
                logger.exception(
                    "Failed to classify tool event risk", extra={"event": "tool_risk_classification_error"}
                )

        summary = ""
        if event.get("error"):
            summary = str(event.get("error"))
        elif risk_reason:
            summary = risk_reason

        self._append_timeline_event(
            {
                "event_type": "tool",
                "tool_name": tool_name,
                "risk_level": risk_level,
                "duration_ms": int(event.get("duration_ms", 0) or 0),
                "outcome": str(event.get("outcome", "unknown")),
                "summary": summary[:240],
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )

    def _append_timeline_event(self, entry: dict[str, Any]) -> None:
        row = dict(entry)
        row["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        self._action_timeline.append(row)
        overflow = len(self._action_timeline) - self._timeline_limit
        if overflow > 0:
            del self._action_timeline[:overflow]

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
        if event_name in {"llm_budget_warning", "llm_budget_hard_stop"}:
            self._append_timeline_event(
                {
                    "event_type": "router",
                    "tool_name": "llm_router",
                    "risk_level": "high" if event_name == "llm_budget_warning" else "critical",
                    "duration_ms": 0,
                    "outcome": event_name,
                    "summary": str(event.get("provider", "budget event")),
                }
            )
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
        self._append_timeline_event(
            {
                "event_type": "risk",
                "tool_name": str(event.get("tool_name", "unknown")),
                "risk_level": risk_level,
                "duration_ms": 0,
                "outcome": "approval_required",
                "summary": str(event.get("reason", "approval required"))[:240],
            }
        )
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
        self._append_timeline_event(
            {
                "event_type": "loop",
                "tool_name": str(event.get("source", "loop")),
                "risk_level": priority,
                "duration_ms": 0,
                "outcome": category,
                "summary": f"{title}: {body}"[:240],
            }
        )
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
        self._is_running = True
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
        self._is_running = False
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
        uptime_seconds = max(0, int((datetime.now(timezone.utc) - self._started_at).total_seconds()))
        return {
            "status": "ok",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "runtime": {
                "started_at_utc": self._started_at.isoformat(),
                "uptime_seconds": uptime_seconds,
                "current_task": self.ralph_loop.current_task,
            },
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

    def _build_dashboard_snapshot(self) -> dict[str, Any]:
        health = self._build_health_status()
        runtime = health.get("runtime", {}) if isinstance(health.get("runtime"), dict) else {}
        queue = health.get("queue", {}) if isinstance(health.get("queue"), dict) else {}
        task_limit = max(20, min(100, self.config.task_queue_max_list))
        tasks = self.task_queue.list_tasks(status=None, limit=task_limit)
        usage = self.router.usage_summary()
        provider_metrics = usage.get("providers", {}) if isinstance(usage, dict) else {}

        return {
            "timestamp_utc": health.get("timestamp_utc"),
            "runtime": {
                "status": "running" if self._is_running else "stopped",
                "started_at_utc": runtime.get("started_at_utc"),
                "uptime_seconds": runtime.get("uptime_seconds"),
                "uptime_human": self._format_uptime_human(int(runtime.get("uptime_seconds", 0) or 0)),
                "current_task": runtime.get("current_task"),
            },
            "providers": health.get("providers", {}),
            "provider_metrics": provider_metrics,
            "queue": queue,
            "tasks": tasks,
            "actions": list(reversed(self._action_timeline[-100:])),
            "config": {
                "loop_enabled": self.config.loop_enabled,
                "task_queue_enabled": self.config.task_queue_enabled,
                "notifications_enabled": self.config.notifications_enabled,
                "notification_mode": self.config.notification_delivery_mode,
                "dashboard_enabled": self.config.dashboard_enabled,
                "healthcheck_port": self.config.healthcheck_port,
                "vector_memory_enabled": self.config.vector_memory_enabled,
            },
        }

    @staticmethod
    def _format_uptime_human(seconds: int) -> str:
        total = max(0, seconds)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours or parts:
            parts.append(f"{hours}h")
        if minutes or parts:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        return " ".join(parts)
