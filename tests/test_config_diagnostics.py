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


def test_config_diagnostics_provider_not_configured(tmp_path: Path):
    """Providers without API key show status 'not configured'."""
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
    not_configured = [p for p in report.providers if p.status == "not configured"]
    assert len(not_configured) >= 3
    assert all(not p.configured for p in not_configured)
    assert any("missing API key" in p.detail for p in not_configured)


def test_config_diagnostics_provider_fetcher_raises(tmp_path: Path):
    """When fetcher raises, provider status is 'unreachable'."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    def _fetcher_fail(_method: str, url: str, *_args, **_kwargs):
        if "openai.com" in url:
            raise OSError("Connection refused")
        return (200, "{}")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )

    report = run_config_diagnostics(config, fetcher=_fetcher_fail)
    openai_check = next(p for p in report.providers if p.provider == "openai")
    assert openai_check.status == "unreachable"
    assert "Connection refused" in openai_check.detail
    assert openai_check.latency_ms is not None


def test_config_diagnostics_provider_http_non_2xx(tmp_path: Path):
    """Provider returning non-2xx gets status 'unreachable'."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (503, "Service Unavailable"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    openai_check = next(p for p in report.providers if p.provider == "openai")
    assert openai_check.status == "unreachable"
    assert "503" in openai_check.detail


def test_config_diagnostics_telegram_getme_non_200(tmp_path: Path):
    """Telegram getMe non-200 yields invalid status and error."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bad-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (200, "{}"),
            ("GET", "https://api.telegram.org/botbad-token/getMe"): (401, "Unauthorized"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.telegram_status == "invalid"
    assert any("Telegram" in err for err in report.errors)


def test_config_diagnostics_telegram_getme_raises(tmp_path: Path):
    """Telegram getMe exception yields unreachable and warning."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    def _fetcher(method: str, url: str, *_args, **_kwargs):
        if "telegram" in url.lower():
            raise TimeoutError("timed out")
        return (200, "{}")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )

    report = run_config_diagnostics(config, fetcher=_fetcher)
    assert report.telegram_status == "unreachable"
    assert any("timed out" in w for w in report.warnings)


def test_config_diagnostics_skills_dir_missing(tmp_path: Path):
    """Missing skills directory produces invalid status and error."""
    missing_skills = tmp_path / "nonexistent_skills"
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(missing_skills),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (200, "{}"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.skills_status == "invalid"
    assert "directory does not exist" in report.skills_detail
    assert any("Skills directory missing" in err for err in report.errors)


def test_config_diagnostics_skills_dir_not_a_directory(tmp_path: Path):
    """Skills path that is a file (not a dir) produces invalid status."""
    file_as_skills = tmp_path / "skills_file"
    file_as_skills.write_text("not a dir", encoding="utf-8")
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(file_as_skills),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (200, "{}"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.skills_status == "invalid"
    assert "not a directory" in report.skills_detail


def test_config_diagnostics_soul_not_a_file(tmp_path: Path):
    """SOUL path that is a directory yields warning."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_as_dir = tmp_path / "SOUL_dir"
    soul_as_dir.mkdir()

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_as_dir),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (200, "{}"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.soul_status == "warning"
    assert "not a file" in report.soul_detail
    assert any("SOUL path" in w for w in report.warnings)


def test_config_diagnostics_warning_when_no_reachable_providers(tmp_path: Path):
    """When all configured providers return non-2xx, warning is added."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("identity", encoding="utf-8")

    config = CueConfig(
        openai_api_key="sk-x",
        openai_base_url="https://api.openai.com",
        lmstudio_base_url="",
        telegram_bot_token="bot-token",
        telegram_admin_chat_id=12345,
        skills_dir=str(skills_dir),
        soul_md_path=str(soul_path),
    )
    fetcher = _fetcher_factory(
        {
            ("GET", "https://api.openai.com/v1/models"): (503, "down"),
            ("GET", "https://api.telegram.org/botbot-token/getMe"): (200, "{}"),
        }
    )

    report = run_config_diagnostics(config, fetcher=fetcher)
    assert report.exit_code == 0
    assert any("No configured LLM providers are currently reachable" in w for w in report.warnings)
