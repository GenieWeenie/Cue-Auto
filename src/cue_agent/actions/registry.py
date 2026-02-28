"""Wraps EAP ToolRegistry and auto-registers built-in tools."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Dict, cast

from environment.tool_registry import ToolRegistry

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


class ActionRegistry:
    """Wraps EAP ToolRegistry and registers CueAgent's built-in tools."""

    def __init__(self, telegram_bot: Any = None) -> None:
        self.eap_registry = ToolRegistry()
        self._skill_tools: dict[str, list[str]] = {}  # skill_name -> [tool_names]
        self._register_builtins(telegram_bot)

    def _register_builtins(self, telegram_bot: Any = None) -> None:
        if telegram_bot is not None:
            tg_func: Callable[..., dict[str, Any]] = partial(send_telegram, bot=telegram_bot)
        else:
            tg_func = send_telegram

        self.eap_registry.register("send_telegram", tg_func, SEND_TELEGRAM_SCHEMA)
        self.eap_registry.register("web_search", web_search, WEB_SEARCH_SCHEMA)
        self.eap_registry.register("read_file", read_file, READ_FILE_SCHEMA)
        self.eap_registry.register("write_file", write_file, WRITE_FILE_SCHEMA)
        self.eap_registry.register("run_shell", run_shell, RUN_SHELL_SCHEMA)
        logger.info("Registered %d built-in tools", len(self.eap_registry._tools))

    def load_skills(self, skills: dict[str, LoadedSkill]) -> list[str]:
        """Register all tools from loaded skills."""
        registered: list[str] = []
        for skill in skills.values():
            tool_names: list[str] = []
            for tool in skill.tools:
                self.eap_registry.register(tool.name, tool.func, tool.schema)
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
