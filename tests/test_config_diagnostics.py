"""Tests for startup diagnostics and check-config reporting."""

from __future__ import annotations

from pathlib import Path

from cue_agent.config import CueConfig
from cue_agent.config_diagnostics import run_config_diagnostics


def _fetcher_factory(responses: dict[tuple[str, str], tuple[int, str]]):
    def _fetcher(
        method: str,
        url: str,
        headers: dict[str, str] | None,
        json_payload: dict | None,
        timeout_seconds: float,
    ) -> tuple[int, str]:
        del headers, json_payload, timeout_seconds
        key = (method, url)
        if key not in responses:
            raise RuntimeError(f"unexpected request: {method} {url}")
        return responses[key]

    return _fetcher


def test_config_diagnostics_pass_with_openai_and_telegram(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (200, "{}"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.exit_code == 0
    assert report.errors == []
    assert report.telegram_status == "ok"
    assert any(p.provider == "openai" and p.status == "ok" for p in report.providers)

    rendered = report.to_text()
    assert "provider      | status" in rendered
    assert "Result: PASS" in rendered


def test_config_diagnostics_fails_when_telegram_missing(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="",
        telegram_admin_chat_id=0,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory({("GET", "https://api.openai.com/v1/models"): (200, "{}")})

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.exit_code == 1
    assert any("CUE_TELEGRAM_BOT_TOKEN" in err for err in report.errors)
    assert any("CUE_TELEGRAM_ADMIN_CHAT_ID" in err for err in report.errors)


def test_config_diagnostics_warns_if_soul_missing(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    missing_soul = tmp_path / "MISSING_SOUL.md"

    config = CueConfig(
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(missing_soul),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (200, "{}"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.exit_code == 0
    assert report.soul_status == "warning"
    assert any("SOUL.md not found" in warning for warning in report.warnings)


def test_config_diagnostics_fails_without_any_provider(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="",
        anthropic_api_key="",
        openrouter_api_key="",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory({("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}")})

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.exit_code == 1
    assert any("No LLM provider configured" in err for err in report.errors)
