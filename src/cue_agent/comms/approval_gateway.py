"""Telegram inline-keyboard approval flow for HITL gating."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


class ApprovalGateway:
    """Sends approve/deny buttons to Telegram and waits for the response."""

    def __init__(self, bot: Bot, admin_chat_id: int):
        self._bot = bot
        self._admin_chat_id = admin_chat_id
        self._pending: dict[str, asyncio.Future[bool]] = {}

    async def request_approval(
        self,
        action_description: str,
        step_id: str,
        timeout: int = 300,
    ) -> bool:
        """Send an approval request and block until approved/denied or timeout."""
        approval_id = f"approval_{step_id}_{uuid4().hex[:6]}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton("Deny", callback_data=f"deny:{approval_id}"),
            ]
        ])

        await self._bot.send_message(
            chat_id=self._admin_chat_id,
            text=f"**APPROVAL REQUIRED**\n\n{action_description}\n\nStep: `{step_id}`",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending[approval_id] = future

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Approval %s timed out after %ds — defaulting to deny", approval_id, timeout)
            return False
        finally:
            self._pending.pop(approval_id, None)

    async def handle_callback(self, approval_id: str, approved: bool) -> None:
        """Resolve a pending approval future from a Telegram callback."""
        future = self._pending.get(approval_id)
        if future and not future.done():
            future.set_result(approved)
            logger.info("Approval %s resolved: %s", approval_id, "approved" if approved else "denied")
