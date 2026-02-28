"""Tests for message normalization."""

from unittest.mock import MagicMock
from datetime import datetime, timezone

from cue_agent.comms.normalizer import MessageNormalizer


def _make_telegram_update(text: str = "Hello") -> MagicMock:
    update = MagicMock()
    update.effective_message.text = text
    update.effective_message.chat_id = 12345
    update.effective_message.message_id = 1
    update.effective_message.date = datetime(2025, 1, 1, tzinfo=timezone.utc)
    update.effective_message.reply_to_message = None
    update.effective_user.id = 99
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    return update


def test_normalize_telegram():
    update = _make_telegram_update("Hello world")
    msg = MessageNormalizer.normalize_telegram(update)
    assert msg is not None
    assert msg.platform == "telegram"
    assert msg.chat_id == "12345"
    assert msg.text == "Hello world"
    assert msg.username == "testuser"


def test_normalize_telegram_no_message():
    update = MagicMock()
    update.effective_message = None
    msg = MessageNormalizer.normalize_telegram(update)
    assert msg is None


def test_normalize_telegram_document_attachment():
    update = _make_telegram_update("")
    update.effective_message.text = None
    update.effective_message.caption = None
    update.effective_message.document = MagicMock(
        file_id="doc-1",
        file_name="report.txt",
        mime_type="text/plain",
    )
    update.effective_message.photo = []

    msg = MessageNormalizer.normalize_telegram(update)
    assert msg is not None
    assert msg.text == "/file"
    assert msg.raw["attachment"]["type"] == "document"
    assert msg.raw["attachment"]["file_name"] == "report.txt"
