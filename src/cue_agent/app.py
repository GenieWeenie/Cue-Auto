"""Application orchestrator — wires all 6 blocks together."""

from __future__ import annotations

import asyncio
import logging
import signal
from functools import partial
from pathlib import Path

from environment.executor import AsyncLocalExecutor
from protocol.state_manager import StateManager

from cue_agent.actions.registry import ActionRegistry
from cue_agent.brain.cue_brain import CueBrain
from cue_agent.brain.llm_router import LLMRouter
from cue_agent.brain.soul_loader import SoulLoader
from cue_agent.comms.approval_gateway import ApprovalGateway
from cue_agent.comms.models import UnifiedMessage, UnifiedResponse
from cue_agent.comms.telegram_gateway import TelegramGateway
from cue_agent.config import CueConfig
from cue_agent.heartbeat.scheduler import Heartbeat
from cue_agent.heartbeat.tasks import daily_summary, health_check
from cue_agent.loop.ralph_loop import RalphLoop
from cue_agent.memory.session_memory import SessionMemory
from cue_agent.security.approval_gate import ApprovalGate
from cue_agent.security.risk_classifier import RiskClassifier
from cue_agent.skills.loader import SkillLoader
from cue_agent.skills.watcher import SkillWatcher

logger = logging.getLogger(__name__)


class CueApp:
    def __init__(self):
        self.config = CueConfig()
        self._setup_logging()

        # --- Memory (EAP StateManager) ---
        self.state_manager = StateManager(db_path=self.config.state_db_path)

        # --- Brain ---
        self.soul_loader = SoulLoader(self.config.soul_md_path)
        self.router = LLMRouter(self.config)
        self.brain = CueBrain(self.config, self.soul_loader, self.router)

        # --- Memory ---
        self.memory = SessionMemory(self.state_manager)

        # --- Actions ---
        self.actions = ActionRegistry()
        self.executor = AsyncLocalExecutor(self.state_manager, self.actions.eap_registry)

        # --- Security ---
        self.risk_classifier = RiskClassifier(self.config.high_risk_tools)
        self.approval_gateway: ApprovalGateway | None = None
        self.approval_gate = ApprovalGate(self.risk_classifier)

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
            self.approval_gate = ApprovalGate(
                self.risk_classifier,
                approval_gateway=self.approval_gateway,
                tool_name_lookup=self.actions.get_hashed_manifest(),
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
            actions=self.actions,
            executor=self.executor,
            state_manager=self.state_manager,
            approval_gate=self.approval_gate,
            config=self.config,
        )

    def _setup_logging(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    async def _handle_message(self, msg: UnifiedMessage) -> UnifiedResponse:
        """Process an incoming message through the brain."""
        self.memory.add_turn(msg.chat_id, "user", msg.text)
        context = self.memory.get_context(msg.chat_id)
        response_text = self.brain.chat(msg.text, extra_context=context)
        self.memory.add_turn(msg.chat_id, "assistant", response_text)
        return UnifiedResponse(text=response_text, chat_id=msg.chat_id)

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
        logger.info("Tools: %d total (%d skills: %s)",
                     self.actions.tool_count, len(self.actions.skill_names), self.actions.skill_names)

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
                ),
                self.config.daily_summary_cron,
            )
            await self.heartbeat.add_cron_task(
                "health_check",
                partial(health_check, brain=self.brain),
                "*/30 * * * *",
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

        tasks: list[asyncio.Task] = []
        if self.config.loop_enabled:
            tasks.append(asyncio.create_task(self.ralph_loop.run_forever()))

        # Keep running until interrupted
        stop_event = asyncio.Event()

        def _signal_handler():
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
        if self.telegram:
            await self.telegram.stop()
        logger.info("CueAgent shut down")
