"""Built-in tool implementations for CueAgent."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def send_telegram(chat_id: str, text: str, *, bot=None) -> dict:
    """Send a Telegram message. Bot instance injected via functools.partial."""
    if bot is None:
        return {"error": "Telegram bot not configured"}
    import asyncio

    async def _send():
        await bot.send_message(chat_id=int(chat_id), text=text)
        return {"status": "sent", "chat_id": chat_id}

    return asyncio.get_event_loop().run_until_complete(_send())


def web_search(query: str) -> dict:
    """Perform a web search. Placeholder — swap with real API."""
    logger.info("web_search: %s", query)
    return {
        "query": query,
        "results": [],
        "note": "Web search not yet configured. Add a search API integration.",
    }


def read_file(path: str) -> dict:
    """Read a file from the workspace."""
    target = Path(path)
    if not target.exists():
        return {"error": f"File not found: {path}"}
    try:
        content = target.read_text(encoding="utf-8")
        return {"path": path, "content": content, "size_bytes": len(content)}
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str) -> dict:
    """Write content to a file in the workspace."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": path, "size_bytes": len(content), "status": "written"}
    except Exception as e:
        return {"error": str(e)}


def run_shell(command: str, timeout: int = 30) -> dict:
    """Execute a shell command with timeout."""
    logger.info("run_shell: %s", command)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:2000],
        }
    except subprocess.TimeoutExpired:
        return {"command": command, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"command": command, "error": str(e)}
