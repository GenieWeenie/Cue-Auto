"""Built-in tool implementations for CueAgent."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


def _log_tool_execution(
    tool_name: str,
    risk_level: str,
    start_time: float,
    success: bool,
    error: str | None = None,
) -> None:
    duration_ms = int((time.monotonic() - start_time) * 1000)
    logger.info(
        "Tool execution",
        extra={
            "event": "tool_execution",
            "tool_name": tool_name,
            "risk_level": risk_level,
            "duration_ms": duration_ms,
            "success": success,
            "error": error,
        },
    )


def send_telegram(chat_id: str, text: str, *, bot: Any = None) -> dict[str, Any]:
    """Send a Telegram message. Bot instance injected via functools.partial."""
    start = time.monotonic()
    if bot is None:
        _log_tool_execution("send_telegram", "high", start, success=False, error="Telegram bot not configured")
        return {"error": "Telegram bot not configured"}
    import asyncio

    async def _send() -> dict[str, str]:
        attempts = max(1, int(os.getenv("CUE_RETRY_TELEGRAM_ATTEMPTS", "5")))
        base_delay = float(os.getenv("CUE_RETRY_BASE_DELAY_SECONDS", "0.5"))
        max_delay = float(os.getenv("CUE_RETRY_MAX_DELAY_SECONDS", "5.0"))
        jitter = float(os.getenv("CUE_RETRY_JITTER_SECONDS", "0.2"))

        from cue_agent.retry_utils import backoff_delay_seconds

        for attempt in range(1, attempts + 1):
            try:
                await bot.send_message(chat_id=int(chat_id), text=text)
                return {"status": "sent", "chat_id": chat_id}
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                status_code = getattr(exc, "status_code", None)
                message = str(exc).lower()
                retryable = (
                    retry_after is not None
                    or status_code == 429
                    or "timed out" in message
                    or "network" in message
                    or "timeout" in message
                )

                if not retryable or attempt >= attempts:
                    raise

                if retry_after is not None:
                    delay = float(retry_after)
                else:
                    delay = backoff_delay_seconds(
                        attempt,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        jitter=jitter,
                    )
                await asyncio.sleep(delay)
        raise RuntimeError("Telegram send retries exhausted")

    try:
        result = cast(dict[str, Any], asyncio.get_event_loop().run_until_complete(_send()))
    except Exception as exc:
        _log_tool_execution("send_telegram", "high", start, success=False, error=str(exc))
        return {"error": str(exc)}
    _log_tool_execution("send_telegram", "high", start, success=True)
    return result


def web_search(query: str) -> dict[str, Any]:
    """Perform a web search. Placeholder — swap with real API."""
    start = time.monotonic()
    result = {
        "query": query,
        "results": [],
        "note": "Web search not yet configured. Add a search API integration.",
    }
    _log_tool_execution("web_search", "low", start, success=True)
    return result


def read_file(path: str) -> dict[str, Any]:
    """Read a file from the workspace."""
    start = time.monotonic()
    target = Path(path)
    if not target.exists():
        _log_tool_execution("read_file", "low", start, success=False, error=f"File not found: {path}")
        return {"error": f"File not found: {path}"}
    try:
        content = target.read_text(encoding="utf-8")
        result = {"path": path, "content": content, "size_bytes": len(content)}
        _log_tool_execution("read_file", "low", start, success=True)
        return result
    except Exception as e:
        _log_tool_execution("read_file", "low", start, success=False, error=str(e))
        return {"error": str(e)}


def write_file(path: str, content: str) -> dict[str, Any]:
    """Write content to a file in the workspace."""
    start = time.monotonic()
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result = {"path": path, "size_bytes": len(content), "status": "written"}
        _log_tool_execution("write_file", "high", start, success=True)
        return result
    except Exception as e:
        _log_tool_execution("write_file", "high", start, success=False, error=str(e))
        return {"error": str(e)}


def run_shell(command: str, timeout: int = 30) -> dict[str, Any]:
    """Execute a shell command with timeout."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload = {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:2000],
        }
        _log_tool_execution("run_shell", "high", start, success=result.returncode == 0)
        return payload
    except subprocess.TimeoutExpired:
        _log_tool_execution("run_shell", "high", start, success=False, error=f"Timed out after {timeout}s")
        return {"command": command, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        _log_tool_execution("run_shell", "high", start, success=False, error=str(e))
        return {"command": command, "error": str(e)}
