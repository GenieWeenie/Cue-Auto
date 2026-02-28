"""Tests for TelegramGateway behavior and correlation propagation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cue_agent.comms.models import UnifiedMessage, UnifiedResponse
from cue_agent.comms.telegram_gateway import TelegramGateway
from cue_agent.config import CueConfig
from cue_agent.logging_utils import get_correlation_id


class _FakeUpdater:
    def __init__(self):
        self.running = False

    async def start_polling(self):
        self.running = True

    async def stop(self):
        self.running = False


class _FakeBot:
    def __init__(self):
        self.commands = []
        self.actions = []
        self.messages = []
        self.documents = []

    async def set_my_commands(self, commands):
        self.commands = list(commands)

    async def send_chat_action(self, chat_id: int, action: str):
        self.actions.append((chat_id, action))

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)


class _FakeApplication:
    def __init__(self):
        self.handlers = []
        self.updater = _FakeUpdater()
        self.running = False
        self.bot = _FakeBot()

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def initialize(self):
        return None

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        return None

    @classmethod
    def builder(cls):
        class _Builder:
            def token(self, value: str):  # noqa: ARG002
                return self

            def build(self):
                return _FakeApplication()

        return _Builder()


@pytest.mark.asyncio
async def test_gateway_message_sets_correlation(monkeypatch):
    monkeypatch.setattr("cue_agent.comms.telegram_gateway.Application", _FakeApplication)

    observed = {"corr": None}

    async def _on_message(msg: UnifiedMessage) -> UnifiedResponse:
        observed["corr"] = get_correlation_id()
        return UnifiedResponse(text=f"ok:{msg.text}", chat_id=msg.chat_id)

    gateway = TelegramGateway(CueConfig(telegram_bot_token="token"), on_message=_on_message)

    update = SimpleNamespace(
        message=SimpleNamespace(
            chat_id=1,
            reply_text=lambda *a, **k: None,  # noqa: ARG005
        )
    )

    replies = []

    async def _reply_text(text: str, parse_mode: str, reply_markup=None):  # noqa: ARG001
        replies.append((text, parse_mode, reply_markup))
        return None

    update.message.reply_text = _reply_text

    monkeypatch.setattr(
        "cue_agent.comms.telegram_gateway.MessageNormalizer.normalize_telegram",
        lambda _u: UnifiedMessage(
            platform="telegram",
            chat_id="1",
            user_id="2",
            username="tester",
            text="hello",
        ),
    )

    await gateway._handle_message(update, None)
    assert isinstance(observed["corr"], str)
    assert observed["corr"].startswith("tg_")
    assert replies[0][0] == "ok:hello"


@pytest.mark.asyncio
async def test_gateway_callback_routes_to_on_approval(monkeypatch):
    monkeypatch.setattr("cue_agent.comms.telegram_gateway.Application", _FakeApplication)
    seen: list[tuple[str, bool]] = []

    async def _on_message(msg: UnifiedMessage) -> UnifiedResponse:  # noqa: ARG001
        return UnifiedResponse(text="ok", chat_id="1")

    async def _on_approval(approval_id: str, approved: bool):
        seen.append((approval_id, approved))

    gateway = TelegramGateway(CueConfig(telegram_bot_token="token"), _on_message, _on_approval)

    async def _answer(*args, **kwargs):  # noqa: ARG001
        return None

    edited = {"text": ""}

    async def _edit_message_text(text: str, **kwargs):  # noqa: ARG001
        edited["text"] = text

    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            answer=_answer,
            data="approve:approval_1",
            edit_message_text=_edit_message_text,
        )
    )

    await gateway._handle_callback(update, None)
    assert seen == [("approval_1", True)]
    assert edited["text"].startswith("Approved:")


@pytest.mark.asyncio
async def test_gateway_callback_nav_dispatches_command(monkeypatch):
    monkeypatch.setattr("cue_agent.comms.telegram_gateway.Application", _FakeApplication)
    seen: list[str] = []

    async def _on_message(msg: UnifiedMessage) -> UnifiedResponse:
        seen.append(msg.text)
        return UnifiedResponse(text="*Status*", chat_id=msg.chat_id, ui_mode="status")

    gateway = TelegramGateway(CueConfig(telegram_bot_token="token"), _on_message)

    async def _answer(*args, **kwargs):  # noqa: ARG001
        return None

    async def _edit_message_text(text: str, **kwargs):  # noqa: ARG001
        return None

    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            answer=_answer,
            data="nav:status",
            from_user=SimpleNamespace(id=2, username="u", first_name="U"),
            message=SimpleNamespace(chat_id=1, message_id=10, text="old"),
            edit_message_text=_edit_message_text,
        )
    )

    await gateway._handle_callback(update, None)
    assert seen == ["/status"]


@pytest.mark.asyncio
async def test_start_polling_sets_command_menu(monkeypatch):
    monkeypatch.setattr("cue_agent.comms.telegram_gateway.Application", _FakeApplication)

    async def _on_message(msg: UnifiedMessage) -> UnifiedResponse:  # noqa: ARG001
        return UnifiedResponse(text="ok", chat_id="1")

    gateway = TelegramGateway(CueConfig(telegram_bot_token="token"), _on_message)
    await gateway.start_polling()
    assert any(command.command == "status" for command in gateway.app.bot.commands)
    await gateway.stop()
