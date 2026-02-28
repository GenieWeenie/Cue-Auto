"""Startup configuration diagnostics used by the --check-config CLI mode."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from cue_agent.config import CueConfig
from cue_agent.skills.loader import SkillLoader

Fetcher = Callable[[str, str, dict[str, str] | None, dict[str, Any] | None, float], tuple[int, str]]


@dataclass
class ProviderCheck:
    provider: str
    status: str
    model: str
    latency_ms: int | None
    detail: str
    configured: bool


@dataclass
class ConfigCheckReport:
    providers: list[ProviderCheck]
    telegram_status: str
    telegram_detail: str
    skills_status: str
    skills_detail: str
    soul_status: str
    soul_detail: str
    errors: list[str]
    warnings: list[str]
    exit_code: int

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append("CueAgent configuration diagnostics")
        lines.append("")
        lines.append("Provider checks")
        lines.append("provider      | status              | model                        | latency_ms")
        lines.append("--------------+---------------------+------------------------------+-----------")
        for p in self.providers:
            latency = "-" if p.latency_ms is None else str(p.latency_ms)
            lines.append(f"{p.provider:<13}| {p.status:<20}| {p.model:<29}| {latency:>10}")
        lines.append("")
        lines.append(f"Telegram: {self.telegram_status} ({self.telegram_detail})")
        lines.append(f"Skills:   {self.skills_status} ({self.skills_detail})")
        lines.append(f"SOUL.md:  {self.soul_status} ({self.soul_detail})")
        lines.append("")
        if self.errors:
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"- {err}")
        if self.warnings:
            lines.append("Warnings:")
            for warning in self.warnings:
                lines.append(f"- {warning}")
        lines.append("")
        lines.append(f"Result: {'PASS' if self.exit_code == 0 else 'FAIL'} (exit {self.exit_code})")
        return "\n".join(lines)


def _default_fetcher(
    method: str,
    url: str,
    headers: dict[str, str] | None,
    json_payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> tuple[int, str]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.request(method=method, url=url, headers=headers, json=json_payload)
        return response.status_code, response.text


def _provider_status(
    provider: str,
    model: str,
    configured: bool,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    payload: dict[str, Any] | None,
    fetcher: Fetcher,
    timeout_seconds: float,
) -> ProviderCheck:
    if not configured:
        return ProviderCheck(
            provider=provider,
            status="not configured",
            model=model,
            latency_ms=None,
            detail="missing API key or endpoint",
            configured=False,
        )

    start = time.monotonic()
    try:
        status_code, _ = fetcher(method, url, headers, payload, timeout_seconds)
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return ProviderCheck(
            provider=provider,
            status="unreachable",
            model=model,
            latency_ms=latency_ms,
            detail=str(exc),
            configured=True,
        )

    latency_ms = int((time.monotonic() - start) * 1000)
    if 200 <= status_code < 300:
        return ProviderCheck(
            provider=provider,
            status="ok",
            model=model,
            latency_ms=latency_ms,
            detail=f"HTTP {status_code}",
            configured=True,
        )
    return ProviderCheck(
        provider=provider,
        status="unreachable",
        model=model,
        latency_ms=latency_ms,
        detail=f"HTTP {status_code}",
        configured=True,
    )


def run_config_diagnostics(
    config: CueConfig,
    fetcher: Fetcher = _default_fetcher,
) -> ConfigCheckReport:
    errors: list[str] = []
    warnings: list[str] = []

    providers = [
        _provider_status(
            provider="openai",
            model=config.openai_model,
            configured=config.has_openai,
            method="GET",
            url=f"{config.openai_base_url.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {config.openai_api_key}"} if config.has_openai else None,
            payload=None,
            fetcher=fetcher,
            timeout_seconds=5,
        ),
        _provider_status(
            provider="anthropic",
            model=config.anthropic_model,
            configured=config.has_anthropic,
            method="GET",
            url="https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": config.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            }
            if config.has_anthropic
            else None,
            payload=None,
            fetcher=fetcher,
            timeout_seconds=5,
        ),
        _provider_status(
            provider="openrouter",
            model=config.openrouter_model,
            configured=config.has_openrouter,
            method="GET",
            url=f"{config.openrouter_base_url.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {config.openrouter_api_key}"} if config.has_openrouter else None,
            payload=None,
            fetcher=fetcher,
            timeout_seconds=5,
        ),
        _provider_status(
            provider="lmstudio",
            model=config.lmstudio_model,
            configured=bool(config.lmstudio_base_url),
            method="GET",
            url=f"{config.lmstudio_base_url.rstrip('/')}/v1/models",
            headers=None,
            payload=None,
            fetcher=fetcher,
            timeout_seconds=2,
        ),
    ]

    configured_providers = [p for p in providers if p.configured]
    reachable_providers = [p for p in providers if p.status == "ok"]
    if not configured_providers:
        errors.append(
            "No LLM provider configured. Set at least one: "
            "CUE_OPENAI_API_KEY, CUE_ANTHROPIC_API_KEY, CUE_OPENROUTER_API_KEY, "
            "or run LM Studio and set CUE_LMSTUDIO_BASE_URL."
        )
    elif not reachable_providers:
        warnings.append("No configured LLM providers are currently reachable.")

    # Telegram checks
    if not config.telegram_bot_token:
        telegram_status = "invalid"
        telegram_detail = "missing bot token"
        errors.append("Missing CUE_TELEGRAM_BOT_TOKEN (example: 123456:ABCDEF...).")
    else:
        try:
            status_code, _ = fetcher(
                "GET",
                f"https://api.telegram.org/bot{config.telegram_bot_token}/getMe",
                None,
                None,
                5,
            )
            if 200 <= status_code < 300:
                telegram_status = "ok"
                telegram_detail = "token valid (getMe)"
            else:
                telegram_status = "invalid"
                telegram_detail = f"getMe returned HTTP {status_code}"
                errors.append("Telegram token validation failed (getMe).")
        except Exception as exc:
            telegram_status = "unreachable"
            telegram_detail = str(exc)
            warnings.append(f"Could not reach Telegram getMe endpoint: {exc}")

    if config.telegram_admin_chat_id <= 0:
        errors.append("Missing/invalid CUE_TELEGRAM_ADMIN_CHAT_ID (example: 123456789).")

    # Skills checks
    skills_dir = Path(config.skills_dir)
    if not skills_dir.exists():
        skills_status = "invalid"
        skills_detail = "directory does not exist"
        errors.append(f"Skills directory missing: {skills_dir}")
    elif not skills_dir.is_dir():
        skills_status = "invalid"
        skills_detail = "path is not a directory"
        errors.append(f"Skills path is not a directory: {skills_dir}")
    elif not os.access(skills_dir, os.R_OK):
        skills_status = "invalid"
        skills_detail = "directory is not readable"
        errors.append(f"Skills directory is not readable: {skills_dir}")
    else:
        loader = SkillLoader(config.skills_dir)
        discovered = loader.discover()
        skills_status = "ok"
        skills_detail = f"{len(discovered)} discoverable skill(s)"

    # SOUL checks (warning-only if missing)
    soul_path = Path(config.soul_md_path)
    if not soul_path.exists():
        soul_status = "warning"
        soul_detail = "file missing"
        warnings.append(f"SOUL.md not found at {soul_path} (agent can run, but identity prompt is disabled).")
    elif not soul_path.is_file():
        soul_status = "warning"
        soul_detail = "path is not a file"
        warnings.append(f"SOUL path is not a file: {soul_path}")
    else:
        try:
            _ = soul_path.read_text(encoding="utf-8")
            soul_status = "ok"
            soul_detail = "readable"
        except Exception as exc:
            soul_status = "warning"
            soul_detail = f"unreadable: {exc}"
            warnings.append(f"SOUL.md exists but could not be read: {exc}")

    minimum_viable = (
        bool(configured_providers) and bool(config.telegram_bot_token) and config.telegram_admin_chat_id > 0
    )
    if not minimum_viable:
        errors.append("Minimum viable config requires at least one LLM provider plus Telegram token/admin chat ID.")

    return ConfigCheckReport(
        providers=providers,
        telegram_status=telegram_status,
        telegram_detail=telegram_detail,
        skills_status=skills_status,
        skills_detail=skills_detail,
        soul_status=soul_status,
        soul_detail=soul_detail,
        errors=errors,
        warnings=warnings,
        exit_code=0 if not errors else 1,
    )
