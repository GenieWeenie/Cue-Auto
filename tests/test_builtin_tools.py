"""Tests for built-in action tools."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import logging

import cue_agent.actions.builtin_tools as builtin_tools
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


def test_web_search_rejects_empty_query():
    result = web_search("   ")
    assert result["error"] == "Query must not be empty"


def test_web_search_tavily_success(monkeypatch):
    monkeypatch.setenv("CUE_TAVILY_API_KEY", "tv-test")
    monkeypatch.setenv("CUE_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr("cue_agent.actions.builtin_tools._apply_search_rate_limit", lambda: None)

    def _fake_request(**kwargs):  # noqa: ANN003
        assert kwargs["url"] == "https://api.tavily.com/search"
        return {
            "results": [
                {"title": "Cue Agent docs", "url": "https://example.com/cue", "content": "Cue Agent setup"},
                {"title": "Cue Agent blog", "url": "https://example.com/blog", "content": "Cue Agent roadmap"},
            ]
        }

    monkeypatch.setattr("cue_agent.actions.builtin_tools._search_request_json", _fake_request)

    result = web_search("cue agent", max_results=2, include_content=True)
    assert result["provider_used"] == "tavily"
    assert result["providers_attempted"] == ["tavily"]
    assert len(result["results"]) == 2
    assert "content" in result["results"][0]


def test_web_search_fallbacks_to_serpapi(monkeypatch):
    monkeypatch.setenv("CUE_TAVILY_API_KEY", "tv-test")
    monkeypatch.setenv("CUE_SERPAPI_API_KEY", "sp-test")
    monkeypatch.setenv("CUE_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr("cue_agent.actions.builtin_tools._apply_search_rate_limit", lambda: None)

    def _fake_request(**kwargs):  # noqa: ANN003
        if kwargs["url"] == "https://api.tavily.com/search":
            raise RuntimeError("tavily down")
        assert kwargs["url"] == "https://serpapi.com/search.json"
        return {
            "organic_results": [
                {"title": "Cue Agent", "link": "https://example.org/cue", "snippet": "search result"},
            ]
        }

    monkeypatch.setattr("cue_agent.actions.builtin_tools._search_request_json", _fake_request)

    result = web_search("cue", max_results=3)
    assert result["provider_used"] == "serpapi"
    assert result["providers_attempted"] == ["tavily", "serpapi"]
    assert result["errors"]["tavily"] == "tavily down"
    assert result["results"][0]["url"] == "https://example.org/cue"


def test_web_search_fallbacks_to_duckduckgo(monkeypatch):
    monkeypatch.delenv("CUE_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("CUE_SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("CUE_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr("cue_agent.actions.builtin_tools._apply_search_rate_limit", lambda: None)

    def _fake_request(**kwargs):  # noqa: ANN003
        assert kwargs["url"] == "https://api.duckduckgo.com/"
        return {
            "AbstractURL": "https://example.net/overview",
            "AbstractText": "Cue Agent overview",
            "Heading": "Cue Agent",
            "RelatedTopics": [
                {"Text": "Cue Agent docs - docs", "FirstURL": "https://example.net/docs"},
            ],
        }

    monkeypatch.setattr("cue_agent.actions.builtin_tools._search_request_json", _fake_request)

    result = web_search("cue agent", max_results=2)
    assert result["provider_used"] == "duckduckgo"
    assert result["providers_attempted"] == ["tavily", "serpapi", "duckduckgo"]
    assert len(result["results"]) == 2
    assert result["results"][0]["provider"] == "duckduckgo"


def test_web_search_dedupes_normalized_urls(monkeypatch):
    monkeypatch.setenv("CUE_SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr("cue_agent.actions.builtin_tools._apply_search_rate_limit", lambda: None)

    def _fake_request(**kwargs):  # noqa: ANN003
        assert kwargs["url"] == "https://api.duckduckgo.com/"
        return {
            "RelatedTopics": [
                {"Text": "Cue Agent docs - main", "FirstURL": "https://example.io/docs"},
                {"Text": "Cue Agent docs copy - alt", "FirstURL": "https://example.io/docs/"},
            ]
        }

    monkeypatch.setattr("cue_agent.actions.builtin_tools._search_request_json", _fake_request)

    result = web_search("cue docs", max_results=5)
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] in {"https://example.io/docs", "https://example.io/docs/"}


def test_search_rate_limit_waits(monkeypatch):
    monkeypatch.setenv("CUE_SEARCH_RATE_LIMIT_SECONDS", "1.0")
    monkeypatch.setattr(builtin_tools, "_SEARCH_LAST_CALL_AT", 0.0)

    calls: list[float] = []
    mono = iter([0.2, 1.1])
    monkeypatch.setattr("cue_agent.actions.builtin_tools.time.monotonic", lambda: next(mono))
    monkeypatch.setattr("cue_agent.actions.builtin_tools.time.sleep", lambda sec: calls.append(sec))

    builtin_tools._apply_search_rate_limit()
    assert calls == [0.8]


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
