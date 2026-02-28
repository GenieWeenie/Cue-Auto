"""Application orchestrator — wires all 6 blocks together."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from environment.executor import AsyncLocalExecutor
from protocol.state_manager import StateManager

from cue_agent import __version__ as cue_agent_version
from cue_agent.actions.registry import ActionRegistry
from cue_agent.audit import AuditQuery, AuditTrail
from cue_agent.brain.cue_brain import CueBrain
from cue_agent.brain.llm_router import LLMAllProvidersDownError, LLMRouter
from cue_agent.brain.soul_loader import SoulLoader
from cue_agent.comms.approval_gateway import ApprovalGateway
from cue_agent.comms.models import UnifiedMessage, UnifiedResponse
from cue_agent.comms.telegram_gateway import TelegramGateway
from cue_agent.config import CueConfig
from cue_agent.heartbeat.scheduler import Heartbeat
from cue_agent.heartbeat.tasks import cleanup_audit_trail, consolidate_vector_memory, daily_summary, health_check
from cue_agent.health.server import HealthServer
from cue_agent.loop.ralph_loop import RalphLoop
from cue_agent.loop.task_queue import TaskQueue
from cue_agent.logging_utils import correlation_context, get_correlation_id, new_correlation_id, setup_logging
from cue_agent.memory.session_memory import SessionMemory
from cue_agent.memory.vector_memory import VectorMemory
from cue_agent.notifications.manager import NotificationManager
from cue_agent.security.approval_gate import ApprovalGate
from cue_agent.security.risk_classifier import RiskClassifier
from cue_agent.security.user_access import UserAccessStore, has_permission, is_approver
from cue_agent.skills.loader import SkillLoader
from cue_agent.skills.marketplace import SkillMarketplace
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
        self._telegram_runtime_mode = "polling"

        # --- Memory (EAP StateManager) ---
        self.state_manager = StateManager(db_path=self.config.state_db_path)
        self.task_queue = TaskQueue(db_path=self.config.state_db_path)
        self.audit_trail = AuditTrail(db_path=self.config.state_db_path)
        self.user_access = UserAccessStore(db_path=self.config.state_db_path)
        self._bootstrap_access_roles()

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
        self.marketplace = SkillMarketplace(
            index_path=self.config.skills_registry_index_path,
            packages_dir=self.config.skills_registry_packages_dir,
            install_dir=self.config.skills_dir,
            installed_state_path=self.config.skills_registry_state_path,
            cue_agent_version=cue_agent_version,
        )
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

    def _bootstrap_access_roles(self) -> None:
        admin_ids: list[str] = []
        for raw in getattr(self.config, "telegram_admin_user_ids", []):
            value = str(raw).strip()
            if value:
                admin_ids.append(value)
        if getattr(self.config, "telegram_admin_chat_id", 0) > 0:
            admin_ids.append(str(self.config.telegram_admin_chat_id))

        for user_id in admin_ids:
            self.user_access.set_role(user_id, "admin", actor_user_id="system")

        for raw in getattr(self.config, "telegram_operator_user_ids", []):
            user_id = str(raw).strip()
            if not user_id:
                continue
            current = self.user_access.get_user(user_id)
            if current and current.get("role") == "admin":
                continue
            self.user_access.set_role(user_id, "operator", actor_user_id="system")

    def _ensure_user_role(self, msg: UnifiedMessage) -> str:
        row = self.user_access.upsert_user(
            msg.user_id,
            username=msg.username,
            display_name=msg.username,
            default_role="user",
            created_by="telegram",
        )
        role = row.get("role", "user")
        if (
            getattr(self.config, "multi_user_enabled", True)
            and getattr(self.config, "multi_user_bootstrap_first_user", True)
            and not self.user_access.has_any_role("admin")
        ):
            promoted = self.user_access.set_role(msg.user_id, "admin", actor_user_id="bootstrap")
            role = promoted.get("role", role)
            logger.warning(
                "Bootstrapped first user as admin",
                extra={"event": "access_bootstrap_admin", "user_id": msg.user_id},
            )
        return role

    def _conversation_scope_key(self, msg: UnifiedMessage) -> str:
        if not getattr(self.config, "multi_user_enabled", True):
            return msg.chat_id
        return f"{msg.platform}:{msg.chat_id}:{msg.user_id}"

    def _command_permission(self, command: str, parts: list[str]) -> str | None:
        if command == "/help":
            return "help"
        if command == "/status":
            return "status"
        if command == "/skills":
            return "skills"
        if command == "/settings":
            return "settings"
        if command == "/approve":
            return "approve.view"
        if command == "/audit":
            return "audit.export"
        if command == "/tasks":
            return "tasks.view"
        if command == "/usage":
            return "usage"
        if command == "/task":
            return "tasks.manage"
        if command == "/users":
            sub = parts[1].lower() if len(parts) > 1 else ""
            return "users.self" if sub in {"", "me", "whoami", "help", "?"} else "users.manage"
        if command == "/market":
            sub = parts[1].lower() if len(parts) > 1 else ""
            return "skills.marketplace.view" if sub in {"", "help", "search"} else "skills.marketplace.manage"
        return None

    def _deny_access(self, *, msg: UnifiedMessage, role: str, command: str, permission: str) -> UnifiedResponse:
        logger.warning(
            "Access denied",
            extra={
                "event": "access_denied",
                "user_id": msg.user_id,
                "username": msg.username,
                "role": role,
                "command": command,
                "permission": permission,
            },
        )
        self._record_audit_event(
            event_type="authorization",
            action=command,
            risk_level="medium",
            outcome="denied",
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            details={
                "role": role,
                "permission": permission,
                "username": msg.username,
            },
        )
        return UnifiedResponse(
            text=f"Access denied: role `{role}` is not allowed to run `{command}`.",
            chat_id=msg.chat_id,
        )

    async def _handle_message(self, msg: UnifiedMessage) -> UnifiedResponse:
        """Process an incoming message through the brain."""
        if not get_correlation_id():
            with correlation_context(new_correlation_id("tg")):
                return await self._handle_message(msg)

        role = self._ensure_user_role(msg) if getattr(self.config, "multi_user_enabled", True) else "admin"

        self._record_audit_event(
            event_type="conversation",
            action="user_message",
            outcome="received",
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            details={
                "platform": msg.platform,
                "user_id": msg.user_id,
                "username": msg.username,
                "role": role,
                "text_preview": msg.text[:240],
            },
        )

        command_response = self._handle_commands(msg, role)
        if command_response is not None:
            return command_response

        if not has_permission(role, "chat"):
            return self._deny_access(msg=msg, role=role, command="/chat", permission="chat")

        scope_key = self._conversation_scope_key(msg)
        self.memory.add_turn(scope_key, "user", msg.text)
        self.vector_memory.add_turn(scope_key, "user", msg.text)
        context = self.memory.get_context(scope_key)
        vector_context = self.vector_memory.recall_as_context(scope_key, msg.text)
        if vector_context:
            context = f"{context}\n\n{vector_context}" if context else vector_context
        llm_started = time.monotonic()
        try:
            response_text = self.brain.chat(msg.text, extra_context=context)
            self._provider_outage_notified = False
            self._record_audit_event(
                event_type="llm_call",
                action="chat_completion",
                risk_level="low",
                outcome="success",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                duration_ms=int((time.monotonic() - llm_started) * 1000),
                details={"queued_messages": len(self._queued_messages)},
            )
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
            self._record_audit_event(
                event_type="llm_call",
                action="chat_completion",
                risk_level="high",
                outcome="provider_outage",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                duration_ms=int((time.monotonic() - llm_started) * 1000),
                details={"provider_status": exc.provider_status},
            )
            response_text = (
                "All LLM providers are temporarily unavailable. "
                "Your message has been queued and the admin has been notified."
            )
        except Exception as exc:
            self._record_audit_event(
                event_type="error",
                action="chat_completion",
                risk_level="high",
                outcome="error",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                duration_ms=int((time.monotonic() - llm_started) * 1000),
                details={"error": str(exc)},
            )
            raise

        self.memory.add_turn(scope_key, "assistant", response_text)
        self.vector_memory.add_turn(scope_key, "assistant", response_text)
        self._record_audit_event(
            event_type="conversation",
            action="assistant_message",
            outcome="sent",
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            details={"text_preview": response_text[:240]},
        )
        return UnifiedResponse(text=response_text, chat_id=msg.chat_id)

    def _handle_commands(self, msg: UnifiedMessage, role: str) -> UnifiedResponse | None:
        text = msg.text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split()
        command = parts[0].lower()
        permission = self._command_permission(command, parts)
        if getattr(self.config, "multi_user_enabled", True) and permission and not has_permission(role, permission):
            return self._deny_access(msg=msg, role=role, command=command, permission=permission)

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

            if command == "/audit":
                return self._handle_audit_command(msg, parts[1:])

            if command == "/users":
                return self._handle_users_command(msg, parts[1:], role)

            if command == "/market":
                return self._handle_market_command(msg, parts[1:])

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

    def _handle_audit_command(self, msg: UnifiedMessage, args: list[str]) -> UnifiedResponse:
        export_format = "markdown"
        event: str | None = None
        action: str | None = None
        risk: str | None = None
        outcome: str | None = None
        approval: str | None = None
        user_id: str | None = None
        start_utc: str | None = None
        end_utc: str | None = None
        limit = 200

        try:
            for token in args:
                lowered = token.strip().lower()
                if not lowered:
                    continue
                if lowered in {"json", "csv", "markdown", "md"}:
                    export_format = lowered
                    continue
                if lowered.isdigit():
                    limit = int(lowered)
                    continue
                if "=" not in token:
                    raise ValueError(f"Unrecognized audit option: {token}")

                key, value = token.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if not value:
                    continue
                if key == "event":
                    event = value
                elif key == "action":
                    action = value
                elif key == "risk":
                    risk = value
                elif key == "outcome":
                    outcome = value
                elif key == "approval":
                    approval = value
                elif key in {"user", "user_id"}:
                    user_id = value
                elif key in {"start", "from"}:
                    start_utc = value
                elif key in {"end", "to"}:
                    end_utc = value
                elif key == "limit":
                    limit = int(value)
                else:
                    raise ValueError(f"Unsupported audit filter: {key}")
        except ValueError as exc:
            return UnifiedResponse(
                text=f"Audit command error: {exc}\n\n{self._audit_help_text()}",
                chat_id=msg.chat_id,
            )

        query = AuditQuery(
            start_utc=start_utc,
            end_utc=end_utc,
            event=event,
            action=action,
            risk=risk,
            outcome=outcome,
            approval=approval,
            user_id=user_id,
            limit=limit,
        )
        try:
            rows = self.audit_trail.query(query)
            filename, payload, _mime = AuditTrail.export_records(rows, export_format)
        except ValueError as exc:
            return UnifiedResponse(text=f"Audit command error: {exc}", chat_id=msg.chat_id)

        self._record_audit_event(
            event_type="audit_export",
            action=f"telegram_export_{export_format}",
            risk_level=risk or "",
            outcome="success",
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            details={
                "rows": len(rows),
                "filters": {
                    "event": event,
                    "action": action,
                    "risk": risk,
                    "outcome": outcome,
                    "approval": approval,
                    "user_id": user_id,
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "limit": limit,
                },
            },
        )
        return UnifiedResponse(
            text=f"Exported {len(rows)} audit record(s) as {export_format}.",
            chat_id=msg.chat_id,
            document_filename=filename,
            document_bytes=payload,
        )

    def _handle_users_command(self, msg: UnifiedMessage, args: list[str], role: str) -> UnifiedResponse:
        if not getattr(self.config, "multi_user_enabled", True):
            return UnifiedResponse(text="Multi-user access control is disabled.", chat_id=msg.chat_id)

        if not args or args[0].lower() in {"help", "?"}:
            return UnifiedResponse(text=self._users_help_text(), chat_id=msg.chat_id)

        sub = args[0].lower()
        if sub in {"me", "whoami"}:
            row = self.user_access.get_user(msg.user_id) or {}
            return UnifiedResponse(
                text=(
                    "*User Profile*\n"
                    f"- user_id: `{msg.user_id}`\n"
                    f"- username: `{row.get('username', msg.username)}`\n"
                    f"- role: `{row.get('role', role)}`"
                ),
                chat_id=msg.chat_id,
            )

        if sub == "list":
            rows = self.user_access.list_users(limit=100)
            lines = ["*User Access List*"]
            for row in rows:
                lines.append(f"- `{row['user_id']}` role=`{row['role']}` username=`{row['username']}`")
            if not rows:
                lines.append("- none")
            return UnifiedResponse(text="\n".join(lines), chat_id=msg.chat_id)

        if sub in {"add", "set", "role"}:
            if len(args) < 3:
                return UnifiedResponse(
                    text="Usage: /users role <user_id> <admin|operator|user|readonly>", chat_id=msg.chat_id
                )
            target_user_id = args[1]
            target_role = args[2]
            try:
                row = self.user_access.set_role(target_user_id, target_role, actor_user_id=msg.user_id)
            except ValueError as exc:
                return UnifiedResponse(text=f"User command error: {exc}", chat_id=msg.chat_id)
            self._record_audit_event(
                event_type="authorization",
                action="user_role_set",
                outcome="success",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                details={"target_user_id": target_user_id, "new_role": row.get("role", target_role)},
            )
            return UnifiedResponse(
                text=f"Set `{target_user_id}` role to `{row['role']}`.",
                chat_id=msg.chat_id,
            )

        if sub in {"remove", "delete"}:
            if len(args) != 2:
                return UnifiedResponse(text="Usage: /users remove <user_id>", chat_id=msg.chat_id)
            target_user_id = args[1].strip()
            if target_user_id == msg.user_id:
                return UnifiedResponse(text="You cannot remove your own access.", chat_id=msg.chat_id)
            removed = self.user_access.delete_user(target_user_id)
            self._record_audit_event(
                event_type="authorization",
                action="user_removed",
                outcome="success" if removed else "not_found",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                details={"target_user_id": target_user_id},
            )
            if removed:
                return UnifiedResponse(text=f"Removed user `{target_user_id}`.", chat_id=msg.chat_id)
            return UnifiedResponse(text=f"User `{target_user_id}` not found.", chat_id=msg.chat_id)

        return UnifiedResponse(text=self._users_help_text(), chat_id=msg.chat_id)

    def _handle_market_command(self, msg: UnifiedMessage, args: list[str]) -> UnifiedResponse:
        if not args or args[0].lower() in {"help", "?"}:
            return UnifiedResponse(text=self._market_help_text(), chat_id=msg.chat_id)

        sub = args[0].lower()
        try:
            if sub == "search":
                query = " ".join(args[1:]).strip()
                rows = self.marketplace.search(query, limit=10)
                self._record_audit_event(
                    event_type="skill_marketplace",
                    action="search",
                    outcome="success",
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                    details={"query": query, "count": len(rows)},
                )
                if not rows:
                    return UnifiedResponse(text="No marketplace skills found.", chat_id=msg.chat_id)
                lines = ["*Marketplace Skills*"]
                for row in rows:
                    lines.append(
                        f"- `{row['id']}` `{row['latest_version']}` "
                        f"quality=`{row['quality_score']:.2f}` usage=`{row['usage_count']}`"
                    )
                    lines.append(f"  {row['description'][:120]}")
                return UnifiedResponse(text="\n".join(lines), chat_id=msg.chat_id)

            if sub == "install":
                if len(args) < 2:
                    return UnifiedResponse(text="Usage: /market install <skill_id> [version]", chat_id=msg.chat_id)
                skill_id = args[1].strip()
                version = args[2].strip() if len(args) > 2 else None
                result = self.marketplace.install(skill_id, version=version or None, force=True)
                installed_path = Path(str(result["path"]))
                self._reload_marketplace_skill(installed_path)
                self._record_audit_event(
                    event_type="skill_marketplace",
                    action="install",
                    outcome="success",
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                    details={"skill_id": skill_id, "version": result.get("version", "")},
                )
                return UnifiedResponse(
                    text=(
                        f"Installed `{skill_id}` version `{result['version']}`.\n"
                        "Skill written to skills directory and hot-reload is enabled."
                    ),
                    chat_id=msg.chat_id,
                )

            if sub == "update":
                target = args[1].strip() if len(args) > 1 else "all"
                if target == "all":
                    rows = self.marketplace.update_all()
                    self._record_audit_event(
                        event_type="skill_marketplace",
                        action="update_all",
                        outcome="success",
                        chat_id=msg.chat_id,
                        user_id=msg.user_id,
                        details={"count": len(rows)},
                    )
                    lines = ["*Marketplace Updates*"]
                    for row in rows:
                        status = str(row.get("status", "updated"))
                        skill_id = str(row.get("skill_id", "unknown"))
                        if status == "updated":
                            path_value = str(row.get("path", "")).strip()
                            if path_value:
                                self._reload_marketplace_skill(Path(path_value))
                            lines.append(
                                f"- `{skill_id}` `{row.get('previous_version', '?')}` -> `{row.get('version', '?')}`"
                            )
                        elif status == "up_to_date":
                            lines.append(f"- `{skill_id}` up-to-date (`{row.get('version', '?')}`)")
                        else:
                            lines.append(f"- `{skill_id}` error: {row.get('error', 'unknown error')}")
                    return UnifiedResponse(text="\n".join(lines), chat_id=msg.chat_id)

                row = self.marketplace.update(target)
                self._record_audit_event(
                    event_type="skill_marketplace",
                    action="update_one",
                    outcome=str(row.get("status", "updated")),
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                    details={"skill_id": target, "version": row.get("version", "")},
                )
                if row.get("status") == "updated":
                    path_value = str(row.get("path", "")).strip()
                    if path_value:
                        self._reload_marketplace_skill(Path(path_value))
                    return UnifiedResponse(
                        text=(
                            f"Updated `{target}` from `{row.get('previous_version', '?')}` "
                            f"to `{row.get('version', '?')}`."
                        ),
                        chat_id=msg.chat_id,
                    )
                return UnifiedResponse(
                    text=f"`{target}` is already up-to-date (`{row.get('version', '?')}`).",
                    chat_id=msg.chat_id,
                )

            if sub == "validate":
                if len(args) < 2:
                    return UnifiedResponse(text="Usage: /market validate <path>", chat_id=msg.chat_id)
                report = self.marketplace.validate_submission(args[1])
                if report["ok"]:
                    self._record_audit_event(
                        event_type="skill_marketplace",
                        action="validate_submission",
                        outcome="success",
                        chat_id=msg.chat_id,
                        user_id=msg.user_id,
                        details={"path": args[1]},
                    )
                    return UnifiedResponse(
                        text=f"Submission valid for `{report.get('skill_name', '')}`.",
                        chat_id=msg.chat_id,
                    )
                return UnifiedResponse(
                    text=f"Submission invalid: {'; '.join(report['errors'])}",
                    chat_id=msg.chat_id,
                )

            if sub in {"validate-registry", "registry-check"}:
                report = self.marketplace.validate_registry_index()
                if report["ok"]:
                    self._record_audit_event(
                        event_type="skill_marketplace",
                        action="validate_registry",
                        outcome="success",
                        chat_id=msg.chat_id,
                        user_id=msg.user_id,
                        details={"skill_count": report["skill_count"]},
                    )
                    return UnifiedResponse(
                        text=f"Registry valid (`{report['skill_count']}` skill entries).",
                        chat_id=msg.chat_id,
                    )
                return UnifiedResponse(
                    text=f"Registry invalid: {'; '.join(report['errors'][:5])}",
                    chat_id=msg.chat_id,
                )
        except Exception as exc:
            self._record_audit_event(
                event_type="skill_marketplace",
                action=sub,
                outcome="error",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                details={"error": str(exc)},
            )
            return UnifiedResponse(text=f"Marketplace command error: {exc}", chat_id=msg.chat_id)

        return UnifiedResponse(text=self._market_help_text(), chat_id=msg.chat_id)

    def _reload_marketplace_skill(self, path: Path) -> None:
        try:
            skill = self.skill_loader.reload_skill(path)
            self.actions.reload_skill(skill)
        except Exception:
            logger.exception("Marketplace skill reload failed", extra={"event": "marketplace_reload_failed"})

    def _task_help_text(self) -> str:
        return (
            "Task commands:\n"
            "- /usage\n"
            "- /status\n"
            "- /skills\n"
            "- /settings\n"
            "- /approve\n"
            "- /audit [json|csv|markdown] [event=...] [risk=...] [outcome=...] [user=...] "
            "[start=YYYY-MM-DD] [end=YYYY-MM-DD]\n"
            "- /users me | /users list | /users role <user_id> <role> | /users remove <user_id>\n"
            "- /market search <query> | /market install <skill_id> [version] | /market update [skill_id|all]\n"
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
            "- `/audit [json|csv|markdown]` Export audit trail\n"
            "- `/skills` Loaded skills and tool counts\n"
            "- `/usage` Provider usage and spend\n"
            "- `/approve` Pending approval requests\n"
            "- `/users ...` User access and role management\n"
            "- `/market ...` Community skill marketplace commands\n"
            "- `/settings` Runtime settings snapshot"
        )

    @staticmethod
    def _audit_help_text() -> str:
        return (
            "Usage: /audit [json|csv|markdown] [limit] "
            "[event=...] [action=...] [risk=...] [approval=...] [outcome=...] [user=...] "
            "[start=YYYY-MM-DD] [end=YYYY-MM-DD]"
        )

    @staticmethod
    def _users_help_text() -> str:
        return (
            "User commands:\n"
            "- /users me\n"
            "- /users list\n"
            "- /users role <user_id> <admin|operator|user|readonly>\n"
            "- /users remove <user_id>"
        )

    @staticmethod
    def _market_help_text() -> str:
        return (
            "Marketplace commands:\n"
            "- /market search <query>\n"
            "- /market install <skill_id> [version]\n"
            "- /market update [skill_id|all]\n"
            "- /market validate-registry\n"
            "- /market validate <path>"
        )

    def _status_text(self) -> str:
        status = self._build_health_status()
        providers = status.get("providers", {})
        loop = status.get("loop", {})
        queue = status.get("queue", {})
        notifications = status.get("notifications", {})
        memory = status.get("memory", {})
        telegram = status.get("telegram", {})
        access = status.get("access", {})

        provider_line = ", ".join(f"{k}:{v}" for k, v in providers.items()) if isinstance(providers, dict) else "n/a"
        queue_stats = queue.get("task_queue", {}) if isinstance(queue, dict) else {}
        queue_line = ", ".join(f"{k}={v}" for k, v in queue_stats.items()) if isinstance(queue_stats, dict) else "n/a"
        webhook_line = "n/a"
        if isinstance(telegram, dict):
            webhook = telegram.get("webhook", {})
            if isinstance(webhook, dict):
                webhook_line = (
                    f"path={webhook.get('configured_path', 'n/a')} "
                    f"registered={webhook.get('registered', False)} "
                    f"requests={webhook.get('request_count', 0)} "
                    f"rejected={webhook.get('rejected_count', 0)}"
                )
        users_line = "n/a"
        if isinstance(access, dict):
            counts = access.get("role_counts", {})
            if isinstance(counts, dict):
                users_line = (
                    f"total={access.get('total_users', 0)} "
                    f"admin={counts.get('admin', 0)} "
                    f"operator={counts.get('operator', 0)} "
                    f"user={counts.get('user', 0)} "
                    f"readonly={counts.get('readonly', 0)}"
                )
        return (
            "*CueAgent Status*\n"
            f"- Time (UTC): `{status.get('timestamp_utc', 'n/a')}`\n"
            f"- Loop: `enabled={loop.get('enabled', False)}` `running={loop.get('running', False)}`\n"
            f"- Providers: {provider_line}\n"
            f"- Telegram webhook: `{webhook_line}`\n"
            f"- Access: `{users_line}`\n"
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
            f"- Search provider: `{self.config.search_provider}`\n"
            f"- Multi-user access: `{getattr(self.config, 'multi_user_enabled', True)}`\n"
            f"- Audit retention: `{self.config.audit_retention_days}` days "
            f"(`{self.config.audit_cleanup_cron}`)"
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
        self._record_audit_event(
            event_type="tool_execution",
            action=tool_name,
            risk_level=risk_level,
            approval_state="required" if risk_level in {"high", "critical"} else "not_required",
            outcome=str(event.get("outcome", "unknown")),
            duration_ms=int(event.get("duration_ms", 0) or 0),
            details={
                "summary": summary[:240],
                "arguments": arguments if isinstance(arguments, dict) else {},
            },
        )

    def _append_timeline_event(self, entry: dict[str, Any]) -> None:
        row = dict(entry)
        row["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        self._action_timeline.append(row)
        overflow = len(self._action_timeline) - self._timeline_limit
        if overflow > 0:
            del self._action_timeline[:overflow]

    def _record_audit_event(
        self,
        *,
        event_type: str,
        action: str,
        risk_level: str = "",
        approval_state: str = "",
        outcome: str = "",
        chat_id: str = "",
        user_id: str = "",
        run_id: str = "",
        duration_ms: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        correlation_id = get_correlation_id() or "-"
        try:
            self.audit_trail.record_event(
                event_type=event_type,
                action=action,
                correlation_id=correlation_id,
                risk_level=risk_level,
                approval_state=approval_state,
                outcome=outcome,
                chat_id=chat_id,
                user_id=user_id,
                run_id=run_id,
                duration_ms=duration_ms,
                details=details,
            )
        except Exception:
            logger.exception("Failed to record audit event", extra={"event": "audit_record_error"})

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
            self._record_audit_event(
                event_type="llm_router",
                action=event_name,
                risk_level="high" if event_name == "llm_budget_warning" else "critical",
                outcome=event_name,
                details={"event": event},
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
        self._record_audit_event(
            event_type="approval",
            action=str(event.get("tool_name", "unknown")),
            risk_level=risk_level,
            approval_state="required",
            outcome="pending",
            details={"reason": str(event.get("reason", ""))[:240]},
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
        self._record_audit_event(
            event_type="loop_event",
            action=category,
            risk_level=priority,
            outcome=category,
            details={"title": title, "body": body[:240]},
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

    async def _handle_approval(
        self,
        approval_id: str,
        approved: bool,
        actor: UnifiedMessage | None = None,
    ) -> bool:
        """Route Telegram approval callbacks to the approval gateway."""
        actor_user_id = actor.user_id if actor is not None else ""
        actor_chat_id = actor.chat_id if actor is not None else ""
        actor_role = "admin"
        if actor is not None and getattr(self.config, "multi_user_enabled", True):
            actor_role = self._ensure_user_role(actor)
            if not is_approver(actor_role):
                self._record_audit_event(
                    event_type="approval",
                    action=approval_id,
                    risk_level="high",
                    approval_state="rejected",
                    outcome="unauthorized_actor",
                    chat_id=actor_chat_id,
                    user_id=actor_user_id,
                    details={"role": actor_role},
                )
                return False

        self._record_audit_event(
            event_type="approval",
            action=approval_id,
            risk_level="high",
            approval_state="approved" if approved else "rejected",
            outcome="handled",
            chat_id=actor_chat_id,
            user_id=actor_user_id,
            details={"role": actor_role},
        )
        if self.approval_gateway:
            await self.approval_gateway.handle_callback(approval_id, approved)
        return True

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
        if self.config.heartbeat_enabled and self.config.audit_retention_days > 0:
            await self.heartbeat.add_cron_task(
                "audit_retention_cleanup",
                partial(
                    cleanup_audit_trail,
                    audit_trail=self.audit_trail,
                    retention_days=self.config.audit_retention_days,
                ),
                self.config.audit_cleanup_cron,
            )

        # Start skill watcher for hot-reload
        watcher_task = None
        if self.config.skills_hot_reload:
            watcher_task = asyncio.create_task(self.skill_watcher.start())

        try:
            if mode == "polling":
                await self._run_polling()
            elif mode == "webhook":
                await self._run_webhook()
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

        self._telegram_runtime_mode = "polling"
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

    async def _run_webhook(self) -> None:
        """Run Telegram webhook mode with optional Ralph loop."""
        if self.telegram is None:
            logger.error("Telegram not configured — set CUE_TELEGRAM_BOT_TOKEN")
            return

        self._telegram_runtime_mode = "webhook"
        await self.telegram.start_webhook()

        tasks: list[asyncio.Task[Any]] = []
        if self.config.loop_enabled:
            tasks.append(asyncio.create_task(self.ralph_loop.run_forever()))

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
        telegram_diag: dict[str, Any] = {
            "enabled": bool(self.telegram),
            "mode": self._telegram_runtime_mode,
            "webhook": {},
        }
        if self.telegram is not None:
            telegram_diag["webhook"] = self.telegram.webhook_diagnostics()
        multi_user_enabled = getattr(self.config, "multi_user_enabled", True)
        role_counts = self.user_access.role_counts() if multi_user_enabled else {}
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
            "telegram": telegram_diag,
            "access": {
                "multi_user_enabled": multi_user_enabled,
                "total_users": self.user_access.total_users() if multi_user_enabled else 0,
                "role_counts": role_counts,
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
            "telegram": health.get("telegram", {}),
            "access": health.get("access", {}),
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
                "multi_user_enabled": getattr(self.config, "multi_user_enabled", True),
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
