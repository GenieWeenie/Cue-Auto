"""Tests for TelegramGateway behavior and correlation propagation."""

from __future__ import annotations

import asyncio
import json
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
        self.webhook_set_calls = []
        self.webhook_deleted = False

    async def set_my_commands(self, commands):
        self.commands = list(commands)

    async def set_webhook(self, **kwargs):
        self.webhook_set_calls.append(kwargs)
        return True

    async def get_webhook_info(self):
        return SimpleNamespace(
            url="https://example.com/telegram/webhook",
            pending_update_count=0,
            last_error_message="",
            last_error_date=0,
        )

    async def delete_webhook(self, drop_pending_updates: bool = False):  # noqa: ARG002
        self.webhook_deleted = True

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
        self.processed_updates = []

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

    async def process_update(self, update):
        self.processed_updates.append(update)

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
    assert any(command.command == "audit" for command in gateway.app.bot.commands)
    await gateway.stop()


@pytest.mark.asyncio
async def test_start_webhook_requires_secret_token(monkeypatch):
    monkeypatch.setattr("cue_agent.comms.telegram_gateway.Application", _FakeApplication)

    async def _on_message(msg: UnifiedMessage) -> UnifiedResponse:  # noqa: ARG001
        return UnifiedResponse(text="ok", chat_id="1")

    gateway = TelegramGateway(
        CueConfig(
            telegram_bot_token="token",
            telegram_webhook_url="https://example.com/telegram/webhook",
            telegram_webhook_secret_token="",
        ),
        _on_message,
    )
    with pytest.raises(RuntimeError):
        await gateway.start_webhook()


async def _post_webhook(port: int, path: str, payload: dict[str, object], secret: str) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(payload).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/json\r\n"
        f"X-Telegram-Bot-Api-Secret-Token: {secret}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("utf-8") + body
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    status_line = raw.split(b"\r\n", 1)[0].decode("utf-8")
    return int(status_line.split(" ")[1])


@pytest.mark.asyncio
async def test_webhook_server_validates_secret_and_processes_update(monkeypatch):
    monkeypatch.setattr("cue_agent.comms.telegram_gateway.Application", _FakeApplication)

    async def _on_message(msg: UnifiedMessage) -> UnifiedResponse:  # noqa: ARG001
        return UnifiedResponse(text="ok", chat_id="1")

    gateway = TelegramGateway(
        CueConfig(
            telegram_bot_token="token",
            telegram_webhook_url="https://example.com/telegram/webhook",
            telegram_webhook_secret_token="secret-123",
            telegram_webhook_listen_host="127.0.0.1",
            telegram_webhook_listen_port=0,
            telegram_webhook_path="/telegram/webhook",
        ),
        _on_message,
    )
    await gateway.start_webhook()
    try:
        assert gateway.webhook_bound_port is not None
        bad_status = await _post_webhook(
            gateway.webhook_bound_port,
            "/telegram/webhook",
            {"update_id": 1},
            secret="wrong",
        )
        assert bad_status == 401

        ok_status = await _post_webhook(
            gateway.webhook_bound_port,
            "/telegram/webhook",
            {"update_id": 2},
            secret="secret-123",
        )
        assert ok_status == 200
        assert gateway.app.processed_updates
        diagnostics = gateway.webhook_diagnostics()
        assert diagnostics["request_count"] == 1
        assert diagnostics["rejected_count"] == 1
        assert diagnostics["registered"] is True
    finally:
        await gateway.stop()
