from typing import Any, Protocol, TypedDict

class SkillToolDefinition(TypedDict):
    name: str
    schema: dict[str, Any]

class SkillManifest(TypedDict):
    name: str
    description: str
    tools: list[SkillToolDefinition]

class SkillManifestWithDeps(SkillManifest, total=False):
    depends_on: list[str]

class SkillContext(Protocol):
    user_id: str
    chat_id: str
    metadata: dict[str, Any]
