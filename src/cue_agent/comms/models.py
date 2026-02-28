"""Platform-agnostic message models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UnifiedMessage(BaseModel):
    """Normalized inbound message from any platform."""

    platform: str
    chat_id: str
    user_id: str
    username: str = ""
    text: str
    timestamp: datetime = Field(default_factory=_utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)
    reply_to_message_id: str | None = None
    message_thread_id: int | None = None


class UnifiedResponse(BaseModel):
    """Outbound response to send back to the user."""

    text: str
    chat_id: str
    reply_to_message_id: str | None = None
    parse_mode: str = "Markdown"
    ui_mode: str | None = None
    document_filename: str | None = None
    document_bytes: bytes | None = None
