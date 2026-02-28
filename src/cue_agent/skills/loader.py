"""Discovers and loads skills from the skills directory."""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LoadedTool:
    name: str
    func: object
    schema: dict[str, Any]


@dataclass
class LoadedSkill:
    name: str
    description: str
    tools: list[LoadedTool]
    prompt: str | None = None
    config: dict[str, str] | None = None
    source_path: Path = field(default_factory=lambda: Path("."))
    depends_on: list[str] = field(default_factory=list)


class SkillLoader:
    """Discovers and loads skills from a directory.

    Supports two formats:
    - Simple: ``skills/my_skill.py`` with a ``SKILL_MANIFEST`` dict
    - Pack:   ``skills/my_skill/skill.py`` with optional ``prompt.md`` and ``config.yaml``
    """

    def __init__(self, skills_dir: str = "skills"):
        self._skills_dir = Path(skills_dir)
        self._loaded: dict[str, LoadedSkill] = {}
        self._modules: dict[str, ModuleType] = {}

    @property
    def loaded(self) -> dict[str, LoadedSkill]:
        return dict(self._loaded)

    def discover(self) -> list[Path]:
        """Find all loadable skill paths in the skills directory."""
        if not self._skills_dir.exists():
            return []

        paths: list[Path] = []
        for item in sorted(self._skills_dir.iterdir()):
            if item.is_file() and item.suffix == ".py" and not item.name.startswith("_"):
                paths.append(item)
            elif item.is_dir() and (item / "skill.py").exists():
                paths.append(item)

        return paths

    def load_all(self) -> dict[str, LoadedSkill]:
        """Discover and load all skills in dependency order. Returns loaded skills dict."""
        paths = self.discover()
        if not paths:
            return dict(self._loaded)

        # Load each module once and read manifest (name, depends_on) for ordering
        path_to_module: dict[Path, ModuleType] = {}
        path_name_deps: list[tuple[Path, str, list[str]]] = []

        for path in paths:
            try:
                if path.is_file():
                    module_name = f"cue_skills.{path.stem}"
                    module = self._load_module(path, module_name)
                else:
                    skill_file = path / "skill.py"
                    module_name = f"cue_skills.{path.name}"
                    module = self._load_module(skill_file, module_name)
                path_to_module[path] = module
                manifest = getattr(module, "SKILL_MANIFEST", None)
                if manifest is None:
                    raise ValueError("Module has no SKILL_MANIFEST")
                name = manifest["name"]
                depends_on = list(manifest.get("depends_on", []) or [])
                path_name_deps.append((path, name, depends_on))
            except Exception:
                logger.exception("Failed to load skill from %s", path)

        # Resolve load order (dependencies first); raises on circular dependency
        ordered_paths = self._resolve_load_order(path_name_deps)

        # Register skills in dependency order
        for path in ordered_paths:
            mod = path_to_module.get(path)
            if mod is None:
                continue
            try:
                skill = self._extract_skill(mod, path)
                if path.is_dir():
                    prompt_file = path / "prompt.md"
                    if prompt_file.exists():
                        skill.prompt = prompt_file.read_text(encoding="utf-8").strip()
                    config_file = path / "config.yaml"
                    if config_file.exists():
                        skill.config = self._parse_simple_yaml(config_file)
                self._loaded[skill.name] = skill
                self._modules[skill.name] = mod
                logger.info("Loaded skill '%s' (%d tools) from %s", skill.name, len(skill.tools), path)
            except Exception:
                logger.exception("Failed to extract skill from %s", path)

        return dict(self._loaded)

    def load_skill(self, path: Path) -> LoadedSkill:
        """Load a single skill from a .py file or skill pack folder."""
        if path.is_file():
            return self._load_simple_skill(path)
        elif path.is_dir():
            return self._load_skill_pack(path)
        else:
            raise ValueError(f"Not a valid skill path: {path}")

    def reload_skill(self, path: Path) -> LoadedSkill:
        """Reload a skill from disk (re-imports the module)."""
        skill = self.load_skill(path)
        self._loaded[skill.name] = skill
        logger.info("Reloaded skill '%s'", skill.name)
        return skill

    def unload_skill(self, name: str) -> None:
        """Remove a skill from the loaded set."""
        self._loaded.pop(name, None)
        mod_key = self._modules.pop(name, None)
        if mod_key and hasattr(mod_key, "__name__") and mod_key.__name__ in sys.modules:
            del sys.modules[mod_key.__name__]

    def _load_module(self, file_path: Path, module_name: str) -> ModuleType:
        """Import a Python file as a module."""
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {file_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _extract_skill(self, module: ModuleType, source_path: Path) -> LoadedSkill:
        """Extract a LoadedSkill from a module's SKILL_MANIFEST."""
        manifest = getattr(module, "SKILL_MANIFEST", None)
        if manifest is None:
            raise ValueError(f"Module {source_path} has no SKILL_MANIFEST")

        name = manifest["name"]
        description = manifest.get("description", "")
        raw_tools = manifest.get("tools", [])
        depends_on = list(manifest.get("depends_on", []) or [])

        tools: list[LoadedTool] = []
        for raw in raw_tools:
            tool_name = raw["name"]
            schema = raw["schema"]
            func = getattr(module, tool_name, None)
            if func is None:
                raise ValueError(f"Skill '{name}': function '{tool_name}' not found in module")
            tools.append(LoadedTool(name=tool_name, func=func, schema=schema))

        return LoadedSkill(
            name=name,
            description=description,
            tools=tools,
            source_path=source_path,
            depends_on=depends_on,
        )

    def _load_simple_skill(self, file_path: Path) -> LoadedSkill:
        """Load a single .py file as a skill."""
        module_name = f"cue_skills.{file_path.stem}"
        module = self._load_module(file_path, module_name)
        self._modules[file_path.stem] = module
        return self._extract_skill(module, file_path)

    def _load_skill_pack(self, dir_path: Path) -> LoadedSkill:
        """Load a skill pack folder (skill.py + optional prompt.md + config.yaml)."""
        skill_file = dir_path / "skill.py"
        module_name = f"cue_skills.{dir_path.name}"
        module = self._load_module(skill_file, module_name)
        self._modules[dir_path.name] = module
        skill = self._extract_skill(module, dir_path)

        # Load optional prompt.md
        prompt_file = dir_path / "prompt.md"
        if prompt_file.exists():
            skill.prompt = prompt_file.read_text(encoding="utf-8").strip()

        # Load optional config.yaml (simple key: value parsing, no PyYAML dependency)
        config_file = dir_path / "config.yaml"
        if config_file.exists():
            skill.config = self._parse_simple_yaml(config_file)

        return skill

    @staticmethod
    def _resolve_load_order(path_name_deps: list[tuple[Path, str, list[str]]]) -> list[Path]:
        """Return paths in dependency order (dependencies first). Raises ValueError on cycle."""
        name_to_path = {name: path for path, name, _ in path_name_deps}
        adj: dict[str, list[str]] = {name: [] for _, name, _ in path_name_deps}
        in_degree: dict[str, int] = {name: 0 for _, name, _ in path_name_deps}

        for _path, name, deps in path_name_deps:
            for d in deps:
                if d in name_to_path and d != name:
                    adj[d].append(name)
                    in_degree[name] = in_degree[name] + 1

        q: deque[str] = deque(n for n in in_degree if in_degree[n] == 0)
        order: list[str] = []
        while q:
            n = q.popleft()
            order.append(n)
            for neighbor in adj[n]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    q.append(neighbor)

        if len(order) != len(path_name_deps):
            cycle = SkillLoader._find_cycle(path_name_deps)
            raise ValueError(f"Circular skill dependency: {' -> '.join(cycle)}")
        return [name_to_path[n] for n in order]

    @staticmethod
    def _find_cycle(path_name_deps: list[tuple[Path, str, list[str]]]) -> list[str]:
        """Return a list of skill names forming a cycle in the dependency graph."""
        name_to_deps: dict[str, list[str]] = {}
        names = {name for _, name, _ in path_name_deps}
        for _, name, deps in path_name_deps:
            name_to_deps[name] = [d for d in deps if d in names and d != name]
        visiting: set[str] = set()
        path: list[str] = []
        path_set: set[str] = set()
        cycle: list[str] = []

        def dfs(n: str) -> bool:
            visiting.add(n)
            path.append(n)
            path_set.add(n)
            for d in name_to_deps.get(n, []):
                if d in path_set:
                    idx = path.index(d)
                    cycle.extend(path[idx:] + [d])
                    return True
                if d not in visiting and dfs(d):
                    return True
            path.pop()
            path_set.discard(n)
            return False

        for n in names:
            if n not in visiting and dfs(n):
                break
        return cycle if cycle else list(names)[:3]  # fallback

    @staticmethod
    def _parse_simple_yaml(path: Path) -> dict[str, str]:
        """Parse a simple key: value YAML file without PyYAML dependency."""
        config: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                config[key.strip()] = value.strip()
        return config
