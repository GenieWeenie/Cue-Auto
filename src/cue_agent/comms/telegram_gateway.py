"""Telegram bot gateway — handles polling/webhook and routes messages."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cue_agent.comms.models import UnifiedMessage, UnifiedResponse
from cue_agent.comms.normalizer import MessageNormalizer
from cue_agent.config import CueConfig

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[UnifiedMessage], Awaitable[UnifiedResponse]]
OnApprovalCallback = Callable[[str, bool], Awaitable[None]]


class TelegramGateway:
    def __init__(
        self,
        config: CueConfig,
        on_message: OnMessageCallback,
        on_approval: OnApprovalCallback | None = None,
    ):
        self.config = config
        self.on_message = on_message
        self.on_approval = on_approval

        self.app = Application.builder().token(config.telegram_bot_token).build()
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("CueAgent online. Send me a message.")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        unified = MessageNormalizer.normalize_telegram(update)
        if unified is None:
            return

        logger.info("Received message from %s: %s", unified.username, unified.text[:80])
        response = await self.on_message(unified)
        await update.message.reply_text(response.text, parse_mode=response.parse_mode)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        await query.answer()
        data = query.data or ""

        if data.startswith(("approve:", "deny:")):
            action, approval_id = data.split(":", 1)
            approved = action == "approve"
            label = "Approved" if approved else "Denied"
            await query.edit_message_text(f"{label}: {approval_id}")

            if self.on_approval:
                await self.on_approval(approval_id, approved)

    async def start_polling(self) -> None:
        """Start the bot in long-polling mode."""
        logger.info("Starting Telegram bot in polling mode")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()
