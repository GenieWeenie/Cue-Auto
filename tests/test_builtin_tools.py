"""Tests for built-in action tools."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import logging

from cue_agent.actions.builtin_tools import read_file, run_shell, send_telegram, web_search, write_file


def test_send_telegram_without_bot():
    result = send_telegram(chat_id="123", text="hello", bot=None)
    assert result == {"error": "Telegram bot not configured"}


def test_send_telegram_with_bot(monkeypatch):
    class _FakeBot:
        def __init__(self):
            self.calls: list[dict] = []

        async def send_message(self, chat_id: int, text: str):
            self.calls.append({"chat_id": chat_id, "text": text})

    bot = _FakeBot()
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    try:
        result = send_telegram(chat_id="123", text="hello", bot=bot)
        assert result == {"status": "sent", "chat_id": "123"}
        assert bot.calls == [{"chat_id": 123, "text": "hello"}]
    finally:
        loop.close()


def test_send_telegram_retries_on_429_then_succeeds(monkeypatch):
    class _Retry429(Exception):
        status_code = 429

    class _FlakyBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id: int, text: str):  # noqa: ARG002
            self.calls += 1
            if self.calls < 3:
                raise _Retry429("rate limited")

    loop = asyncio.new_event_loop()
    monkeypatch.setenv("CUE_RETRY_TELEGRAM_ATTEMPTS", "5")
    monkeypatch.setenv("CUE_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("CUE_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("CUE_RETRY_JITTER_SECONDS", "0")
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    try:
        bot = _FlakyBot()
        result = send_telegram(chat_id="1", text="x", bot=bot)
        assert result["status"] == "sent"
        assert bot.calls == 3
    finally:
        loop.close()


def test_web_search_placeholder():
    result = web_search("cue agent")
    assert result["query"] == "cue agent"
    assert result["results"] == []
    assert "not yet configured" in result["note"].lower()


def test_read_file_success(tmp_path: Path):
    file_path = tmp_path / "hello.txt"
    file_path.write_text("hello", encoding="utf-8")

    result = read_file(str(file_path))
    assert result["path"] == str(file_path)
    assert result["content"] == "hello"
    assert result["size_bytes"] == 5


def test_read_file_not_found():
    result = read_file("/definitely/not/found.txt")
    assert "error" in result
    assert "File not found" in result["error"]


def test_read_file_read_error(monkeypatch, tmp_path: Path):
    file_path = tmp_path / "x.txt"
    file_path.write_text("x", encoding="utf-8")

    def _explode(self, encoding="utf-8"):  # noqa: ARG001
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", _explode)
    result = read_file(str(file_path))
    assert result["error"] == "boom"


def test_write_file_success(tmp_path: Path):
    file_path = tmp_path / "nested" / "out.txt"
    result = write_file(str(file_path), "abc")

    assert result == {"path": str(file_path), "size_bytes": 3, "status": "written"}
    assert file_path.read_text(encoding="utf-8") == "abc"


def test_write_file_error(monkeypatch, tmp_path: Path):
    file_path = tmp_path / "cannot.txt"

    def _explode(self, content, encoding="utf-8"):  # noqa: ARG001
        raise OSError("write failed")

    monkeypatch.setattr(Path, "write_text", _explode)
    result = write_file(str(file_path), "abc")
    assert result["error"] == "write failed"


def test_run_shell_success(monkeypatch):
    monkeypatch.setattr(
        "cue_agent.actions.builtin_tools.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""),  # noqa: ARG005
    )
    result = run_shell("echo ok")
    assert result["returncode"] == 0
    assert result["stdout"] == "ok"


def test_run_shell_timeout(monkeypatch):
    def _timeout(*a, **k):  # noqa: ARG001, ARG002
        raise subprocess.TimeoutExpired(cmd="sleep 2", timeout=1)

    monkeypatch.setattr("cue_agent.actions.builtin_tools.subprocess.run", _timeout)
    result = run_shell("sleep 2", timeout=1)
    assert result == {"command": "sleep 2", "error": "Timed out after 1s"}


def test_run_shell_generic_error(monkeypatch):
    def _explode(*a, **k):  # noqa: ARG001, ARG002
        raise RuntimeError("bad shell")

    monkeypatch.setattr("cue_agent.actions.builtin_tools.subprocess.run", _explode)
    result = run_shell("boom")
    assert result["command"] == "boom"
    assert result["error"] == "bad shell"


def test_tool_execution_logging_fields(monkeypatch, caplog):
    monkeypatch.setattr(
        "cue_agent.actions.builtin_tools.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""),  # noqa: ARG005
    )

    with caplog.at_level(logging.INFO):
        _ = run_shell("echo ok")

    record = next(r for r in caplog.records if getattr(r, "event", "") == "tool_execution")
    assert record.tool_name == "run_shell"
    assert record.risk_level == "high"
    assert isinstance(record.duration_ms, int)
    assert record.success is True
