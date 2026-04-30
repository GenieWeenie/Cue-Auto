"""Wraps EAP ToolRegistry and auto-registers built-in tools."""

from __future__ import annotations

import inspect
import logging
import time
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Dict, cast

from eap.environment.tool_registry import ToolRegistry

from cue_agent.actions.builtin_tools import (
    read_file,
    run_shell,
    send_telegram,
    web_search,
    write_file,
)
from cue_agent.actions.schemas import (
    READ_FILE_SCHEMA,
    RUN_SHELL_SCHEMA,
    SEND_TELEGRAM_SCHEMA,
    WEB_SEARCH_SCHEMA,
    WRITE_FILE_SCHEMA,
)

if TYPE_CHECKING:
    from cue_agent.skills.loader import LoadedSkill

logger = logging.getLogger(__name__)

ToolEventHandler = Callable[[dict[str, Any]], None]


class ActionRegistry:
    """Wraps EAP ToolRegistry and registers CueAgent's built-in tools."""

    def __init__(self, telegram_bot: Any = None, tool_event_handler: ToolEventHandler | None = None) -> None:
        self.eap_registry = ToolRegistry()
        self._skill_tools: dict[str, list[str]] = {}  # skill_name -> [tool_names]
        self._tool_event_handler = tool_event_handler
        self._register_builtins(telegram_bot)

    def _register_builtins(self, telegram_bot: Any = None) -> None:
        if telegram_bot is not None:
            tg_func: Callable[..., dict[str, Any]] = partial(send_telegram, bot=telegram_bot)
        else:
            tg_func = send_telegram

        self.eap_registry.register(
            "send_telegram",
            self._instrument_tool("send_telegram", tg_func),
            SEND_TELEGRAM_SCHEMA,
        )
        self.eap_registry.register("web_search", self._instrument_tool("web_search", web_search), WEB_SEARCH_SCHEMA)
        self.eap_registry.register("read_file", self._instrument_tool("read_file", read_file), READ_FILE_SCHEMA)
        self.eap_registry.register("write_file", self._instrument_tool("write_file", write_file), WRITE_FILE_SCHEMA)
        self.eap_registry.register("run_shell", self._instrument_tool("run_shell", run_shell), RUN_SHELL_SCHEMA)
        logger.info("Registered %d built-in tools", len(self.eap_registry._tools))

    def load_skills(self, skills: dict[str, LoadedSkill]) -> list[str]:
        """Register all tools from loaded skills."""
        registered: list[str] = []
        for skill in skills.values():
            tool_names: list[str] = []
            for tool in skill.tools:
                tool_func = cast(Callable[..., Any], tool.func)
                self.eap_registry.register(tool.name, self._instrument_tool(tool.name, tool_func), tool.schema)
                tool_names.append(tool.name)
                registered.append(tool.name)
            self._skill_tools[skill.name] = tool_names
            logger.info("Loaded skill '%s' with tools: %s", skill.name, tool_names)
        return registered

    def unload_skill(self, skill_name: str) -> None:
        """Remove a skill's tools from the registry."""
        tool_names = self._skill_tools.pop(skill_name, [])
        for name in tool_names:
            self.eap_registry._tools.pop(name, None)
            self.eap_registry._schemas.pop(name, None)
            self.eap_registry._hashes.pop(name, None)
        if tool_names:
            logger.info("Unloaded skill '%s' (removed tools: %s)", skill_name, tool_names)

    def reload_skill(self, skill: LoadedSkill) -> None:
        """Unload then re-register a skill."""
        self.unload_skill(skill.name)
        self.load_skills({skill.name: skill})

    def get_hashed_manifest(self) -> Dict[str, str]:
        return cast(Dict[str, str], self.eap_registry.get_hashed_manifest())

    def get_agent_manifest(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.eap_registry.get_agent_manifest())

    @property
    def tool_count(self) -> int:
        return len(self.eap_registry._tools)

    @property
    def skill_names(self) -> list[str]:
        return list(self._skill_tools.keys())

    def _instrument_tool(self, tool_name: str, tool_func: Callable[..., Any]) -> Callable[..., Any]:
        if self._tool_event_handler is None:
            return tool_func
        if inspect.iscoroutinefunction(tool_func):
            return self._instrument_async_tool(tool_name, tool_func)
        return self._instrument_sync_tool(tool_name, tool_func)

    def _instrument_sync_tool(self, tool_name: str, tool_func: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            error: str | None = None
            try:
                return tool_func(*args, **kwargs)
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                self._emit_tool_event(
                    tool_name=tool_name,
                    arguments=kwargs,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    outcome="error" if error else "success",
                    error=error,
                )

        return _wrapped

    def _instrument_async_tool(self, tool_name: str, tool_func: Callable[..., Any]) -> Callable[..., Any]:
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            error: str | None = None
            try:
                return await tool_func(*args, **kwargs)
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                self._emit_tool_event(
                    tool_name=tool_name,
                    arguments=kwargs,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    outcome="error" if error else "success",
                    error=error,
                )

        return _wrapped

    def _emit_tool_event(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        duration_ms: int,
        outcome: str,
        error: str | None,
    ) -> None:
        if self._tool_event_handler is None:
            return
        self._tool_event_handler(
            {
                "event": "tool_execution",
                "tool_name": tool_name,
                "arguments": self._sanitize_arguments(arguments),
                "duration_ms": duration_ms,
                "outcome": outcome,
                "error": error,
            }
        )

    def _sanitize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in arguments.items():
            if isinstance(value, (int, float, bool)) or value is None:
                sanitized[key] = value
                continue
            if isinstance(value, str):
                sanitized[key] = value[:240]
                continue
            sanitized[key] = str(value)[:240]
        return sanitized
