"""Testing utilities for isolated skill validation."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any


@dataclass
class MockSkillContext:
    """Minimal mocked context object for skill tests."""

    user_id: str = "test-user"
    chat_id: str = "test-chat"
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillTestHarness:
    """Loads and exercises a skill module in isolation."""

    def __init__(self, module: ModuleType) -> None:
        self._module = module

    @classmethod
    def from_path(cls, path: str | Path) -> SkillTestHarness:
        source = Path(path)
        spec = importlib.util.spec_from_file_location(f"cue_skill_test_{uuid.uuid4().hex}", source)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not import skill module from {source}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return cls(module)

    def manifest(self) -> dict[str, Any]:
        manifest = getattr(self._module, "SKILL_MANIFEST", None)
        if manifest is None:
            raise ValueError("Skill module does not define SKILL_MANIFEST")
        if not isinstance(manifest, dict):
            raise TypeError("SKILL_MANIFEST must be a dict")
        return manifest

    def list_tools(self) -> list[str]:
        tools = self.manifest().get("tools", [])
        return [tool["name"] for tool in tools if isinstance(tool, dict) and "name" in tool]

    def get_tool(self, name: str) -> object:
        func = getattr(self._module, name, None)
        if func is None:
            raise KeyError(f"Tool function '{name}' not found")
        return func

    def run_tool(self, name: str, *, context: MockSkillContext | None = None, **kwargs: Any) -> Any:
        func = self.get_tool(name)
        signature = inspect.signature(func)
        if "context" in signature.parameters and "context" not in kwargs:
            kwargs["context"] = context or MockSkillContext()
        if inspect.iscoroutinefunction(func):
            return asyncio.run(func(**kwargs))
        return func(**kwargs)

    async def run_tool_async(self, name: str, *, context: MockSkillContext | None = None, **kwargs: Any) -> Any:
        """Async variant — use inside an already-running event loop."""
        func = self.get_tool(name)
        signature = inspect.signature(func)
        if "context" in signature.parameters and "context" not in kwargs:
            kwargs["context"] = context or MockSkillContext()
        if inspect.iscoroutinefunction(func):
            return await func(**kwargs)
        return func(**kwargs)
