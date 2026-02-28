"""Telegram bot gateway — handles polling/webhook and routes messages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction
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
from cue_agent.logging_utils import correlation_context, new_correlation_id

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[UnifiedMessage], Awaitable[UnifiedResponse]]
OnApprovalCallback = Callable[[str, bool], Awaitable[None]]

_COMMAND_HELP: list[tuple[str, str]] = [
    ("help", "Show commands and quick actions"),
    ("status", "Runtime status dashboard"),
    ("tasks", "Task queue view"),
    ("skills", "Loaded skills"),
    ("usage", "LLM usage and spend"),
    ("approve", "Pending approvals"),
    ("settings", "Current runtime settings"),
]

_CALLBACK_TO_COMMAND: dict[str, str] = {
    "help": "/help",
    "status": "/status",
    "tasks": "/tasks",
    "skills": "/skills",
    "usage": "/usage",
    "approve": "/approve",
    "settings": "/settings",
}

_MAX_MESSAGE_CHARS = 3500


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
        self.app.add_handler(CommandHandler("help", self._handle_command_message))
        self.app.add_handler(CommandHandler("status", self._handle_command_message))
        self.app.add_handler(CommandHandler("task", self._handle_command_message))
        self.app.add_handler(CommandHandler("tasks", self._handle_command_message))
        self.app.add_handler(CommandHandler("skills", self._handle_command_message))
        self.app.add_handler(CommandHandler("usage", self._handle_command_message))
        self.app.add_handler(CommandHandler("approve", self._handle_command_message))
        self.app.add_handler(CommandHandler("settings", self._handle_command_message))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        self.app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, self._handle_message))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if update.message is None:
            return
        await update.message.reply_text("CueAgent online. Send me a message.")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        unified = MessageNormalizer.normalize_telegram(update)
        if unified is None:
            return

        corr_id = new_correlation_id("tg")
        with correlation_context(corr_id):
            logger.info(
                "Received Telegram message",
                extra={
                    "event": "telegram_message_received",
                    "chat_id": unified.chat_id,
                    "user_id": unified.user_id,
                    "username": unified.username,
                    "text_preview": unified.text[:80],
                },
            )
            response = await self._run_with_progress(
                chat_id=unified.chat_id,
                action=ChatAction.TYPING,
                task=self.on_message(unified),
            )
            await self._send_response(update, response)

    async def _handle_command_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._handle_message(update, context)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return

        await query.answer()
        data = query.data or ""

        if data.startswith(("approve:", "reject:", "deny:")):
            action, approval_id = data.split(":", 1)
            approved = action == "approve"
            label = "Approved" if approved else "Rejected"
            await query.edit_message_text(
                f"{label}: {approval_id}",
                reply_markup=self._approval_result_keyboard(),
            )
            if self.on_approval:
                await self.on_approval(approval_id, approved)
            return

        if data.startswith("details:"):
            _, approval_id = data.split(":", 1)
            message = query.message
            detail_text = ""
            if message is not None:
                detail_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
            preview = detail_text[:180] if detail_text else f"Approval ID: {approval_id}"
            with suppress(Exception):
                await query.answer(text=preview, show_alert=True)
            return

        if data.startswith("nav:"):
            _, nav = data.split(":", 1)
            command = _CALLBACK_TO_COMMAND.get(nav)
            if command:
                await self._dispatch_callback_command(query, command)
            return

        if data == "tasks:download":
            await self._dispatch_callback_command(query, "/tasks download")
            return

        if data == "approve:list":
            await self._dispatch_callback_command(query, "/approve")
            return

    async def _dispatch_callback_command(self, query: Any, command: str) -> None:
        message = query.message
        if message is None:
            return

        unified = UnifiedMessage(
            platform="telegram",
            chat_id=str(message.chat_id),
            user_id=str(query.from_user.id) if query.from_user else "unknown",
            username=(query.from_user.username or query.from_user.first_name or "unknown")
            if query.from_user
            else "unknown",
            text=command,
            raw={"message_id": message.message_id, "source": "callback"},
        )
        response = await self._run_with_progress(
            chat_id=unified.chat_id,
            action=ChatAction.TYPING,
            task=self.on_message(unified),
        )
        await self._send_response(query, response, from_callback=True)

    async def _run_with_progress(
        self,
        *,
        chat_id: str,
        action: str,
        task: Awaitable[UnifiedResponse],
    ) -> UnifiedResponse:
        typing_task = asyncio.create_task(self._typing_indicator(chat_id=chat_id, action=action))
        try:
            return await task
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    async def _typing_indicator(self, *, chat_id: str, action: str) -> None:
        while True:
            with suppress(Exception):
                await self.app.bot.send_chat_action(chat_id=int(chat_id), action=action)
            await asyncio.sleep(2.0)

    async def _send_response(
        self, update_or_query: Any, response: UnifiedResponse, *, from_callback: bool = False
    ) -> None:
        keyboard = self._build_inline_keyboard(response.ui_mode)
        chat_id = int(response.chat_id)
        text_chunks = self._chunk_text(response.text)

        if from_callback:
            query = update_or_query
            if text_chunks and response.document_bytes is None:
                with suppress(Exception):
                    await query.edit_message_text(
                        text_chunks[0],
                        parse_mode=response.parse_mode,
                        reply_markup=keyboard,
                    )
                for extra in text_chunks[1:]:
                    await self.app.bot.send_message(chat_id=chat_id, text=extra, parse_mode=response.parse_mode)
            else:
                for chunk in text_chunks:
                    await self.app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=response.parse_mode)
        else:
            update = update_or_query
            if update.message is not None and text_chunks:
                await update.message.reply_text(
                    text_chunks[0],
                    parse_mode=response.parse_mode,
                    reply_markup=keyboard,
                )
                for extra in text_chunks[1:]:
                    await update.message.reply_text(extra, parse_mode=response.parse_mode)
            else:
                for chunk in text_chunks:
                    await self.app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=response.parse_mode)

        if response.document_bytes is not None:
            filename = response.document_filename or "cue-agent-output.txt"
            document = InputFile(response.document_bytes, filename=filename)
            await self.app.bot.send_document(chat_id=chat_id, document=document)

    def _build_inline_keyboard(self, ui_mode: str | None) -> InlineKeyboardMarkup | None:
        if ui_mode == "status":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Refresh Status", callback_data="nav:status"),
                        InlineKeyboardButton("Tasks", callback_data="nav:tasks"),
                    ],
                    [
                        InlineKeyboardButton("Skills", callback_data="nav:skills"),
                        InlineKeyboardButton("Usage", callback_data="nav:usage"),
                    ],
                    [InlineKeyboardButton("Settings", callback_data="nav:settings")],
                ]
            )
        if ui_mode == "tasks":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Refresh Tasks", callback_data="nav:tasks"),
                        InlineKeyboardButton("Download JSON", callback_data="tasks:download"),
                    ],
                    [InlineKeyboardButton("Status", callback_data="nav:status")],
                ]
            )
        if ui_mode == "skills":
            return InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Refresh Skills", callback_data="nav:skills")],
                    [InlineKeyboardButton("Status", callback_data="nav:status")],
                ]
            )
        if ui_mode == "help":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Status", callback_data="nav:status"),
                        InlineKeyboardButton("Tasks", callback_data="nav:tasks"),
                    ],
                    [
                        InlineKeyboardButton("Skills", callback_data="nav:skills"),
                        InlineKeyboardButton("Settings", callback_data="nav:settings"),
                    ],
                    [InlineKeyboardButton("Pending Approvals", callback_data="approve:list")],
                ]
            )
        if ui_mode == "settings":
            return InlineKeyboardMarkup([[InlineKeyboardButton("Refresh Settings", callback_data="nav:settings")]])
        if ui_mode == "approve":
            return InlineKeyboardMarkup([[InlineKeyboardButton("Refresh Approvals", callback_data="approve:list")]])
        return None

    @staticmethod
    def _approval_result_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Pending Approvals", callback_data="approve:list")]])

    @staticmethod
    def _chunk_text(text: str, max_chars: int = _MAX_MESSAGE_CHARS) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > max_chars:
            split_at = remaining.rfind("\n", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = max_chars
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _configure_command_menu(self) -> None:
        commands = [BotCommand(command=name, description=desc) for name, desc in _COMMAND_HELP]
        with suppress(Exception):
            await self.app.bot.set_my_commands(commands)

    async def start_polling(self) -> None:
        """Start the bot in long-polling mode."""
        logger.info("Starting Telegram bot in polling mode")
        await self.app.initialize()
        await self._configure_command_menu()
        await self.app.start()
        if self.app.updater is None:
            raise RuntimeError("Telegram updater is unavailable for polling mode")
        await self.app.updater.start_polling()
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()
