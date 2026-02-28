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
        if msg is None:
            return None

        user = update.effective_user
        text = msg.text or msg.caption or ""
        raw: dict[str, object] = {"message_id": msg.message_id}

        if msg.document is not None:
            raw["attachment"] = {
                "type": "document",
                "file_id": msg.document.file_id,
                "file_name": msg.document.file_name or "document",
                "mime_type": msg.document.mime_type or "",
            }
            if not text:
                text = "/file"
        elif msg.photo:
            largest = msg.photo[-1]
            raw["attachment"] = {
                "type": "photo",
                "file_id": largest.file_id,
                "file_name": f"photo_{largest.file_unique_id}.jpg",
                "mime_type": "image/jpeg",
            }
            if not text:
                text = "/file"

        if not text:
            return None

        thread_id: int | None = getattr(msg, "message_thread_id", None)
        return UnifiedMessage(
            platform="telegram",
            chat_id=str(msg.chat_id),
            user_id=str(user.id) if user else "unknown",
            username=user.username or user.first_name or "unknown" if user else "unknown",
            text=text,
            timestamp=msg.date or datetime.now(timezone.utc),
            raw=raw,
            reply_to_message_id=str(msg.reply_to_message.message_id) if msg.reply_to_message else None,
            message_thread_id=thread_id,
        )
