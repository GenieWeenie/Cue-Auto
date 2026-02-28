"""Public typing helpers for skill authors."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class SkillToolDefinition(TypedDict):
    """Single tool definition entry in ``SKILL_MANIFEST``."""

    name: str
    schema: dict[str, Any]


class SkillManifest(TypedDict):
    """Shape of the required ``SKILL_MANIFEST`` global in skill modules."""

    name: str
    description: str
    tools: list[SkillToolDefinition]


class SkillManifestWithDeps(SkillManifest, total=False):
    """Optional manifest fields. Use with SkillManifest for full shape."""

    depends_on: list[str]  # Skill names/IDs that must load before this skill


class SkillContext(Protocol):
    """Optional context object that a skill tool may accept."""

    user_id: str
    chat_id: str
    metadata: dict[str, Any]
