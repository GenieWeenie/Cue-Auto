"""Platform-agnostic message models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UnifiedMessage(BaseModel):
    """Normalized inbound message from any platform."""

    platform: str
    chat_id: str
    user_id: str
    username: str = ""
    text: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)
    reply_to_message_id: str | None = None


class UnifiedResponse(BaseModel):
    """Outbound response to send back to the user."""

    text: str
    chat_id: str
    reply_to_message_id: str | None = None
    parse_mode: str = "Markdown"
