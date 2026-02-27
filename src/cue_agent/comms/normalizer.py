"""Translate platform-specific messages into UnifiedMessage format."""

from __future__ import annotations

from datetime import datetime, timezone

from telegram import Update

from cue_agent.comms.models import UnifiedMessage


class MessageNormalizer:
    @staticmethod
    def normalize_telegram(update: Update) -> UnifiedMessage | None:
        """Convert a Telegram Update into a UnifiedMessage."""
        msg = update.effective_message
        if msg is None or msg.text is None:
            return None

        user = update.effective_user
        return UnifiedMessage(
            platform="telegram",
            chat_id=str(msg.chat_id),
            user_id=str(user.id) if user else "unknown",
            username=user.username or user.first_name or "unknown" if user else "unknown",
            text=msg.text,
            timestamp=msg.date or datetime.now(timezone.utc),
            raw={"message_id": msg.message_id},
            reply_to_message_id=str(msg.reply_to_message.message_id) if msg.reply_to_message else None,
        )
