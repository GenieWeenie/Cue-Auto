"""Tests for ActionRegistry and action schemas."""

from __future__ import annotations

from cue_agent.actions.registry import ActionRegistry
from cue_agent.actions.schemas import (
    READ_FILE_SCHEMA,
    RUN_SHELL_SCHEMA,
    SEND_TELEGRAM_SCHEMA,
    WEB_SEARCH_SCHEMA,
    WRITE_FILE_SCHEMA,
)
from cue_agent.skills.loader import LoadedSkill, LoadedTool


def _dummy_tool(**kwargs):  # noqa: ANN003
    return {"ok": kwargs}


def test_builtin_registry_contains_expected_tools():
    registry = ActionRegistry()
    names = set(registry.eap_registry._tools.keys())
    assert names == {"send_telegram", "web_search", "read_file", "write_file", "run_shell"}
    assert registry.tool_count == 5


def test_schema_shapes():
    schemas = [SEND_TELEGRAM_SCHEMA, WEB_SEARCH_SCHEMA, READ_FILE_SCHEMA, WRITE_FILE_SCHEMA, RUN_SHELL_SCHEMA]
    for schema in schemas:
        assert "name" in schema
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["additionalProperties"] is False


def test_load_unload_reload_skill_tools():
    registry = ActionRegistry()
    skill = LoadedSkill(
        name="utility",
        description="utility tools",
        tools=[
            LoadedTool(
                name="echo_tool",
                func=_dummy_tool,
                schema={
                    "name": "echo_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            )
        ],
    )

    loaded = registry.load_skills({"utility": skill})
    assert loaded == ["echo_tool"]
    assert "echo_tool" in registry.eap_registry._tools
    assert "utility" in registry.skill_names

    registry.unload_skill("utility")
    assert "echo_tool" not in registry.eap_registry._tools
    assert "utility" not in registry.skill_names

    registry.reload_skill(skill)
    assert "echo_tool" in registry.eap_registry._tools
    assert "utility" in registry.skill_names


def test_manifest_methods_return_dicts():
    registry = ActionRegistry()
    assert isinstance(registry.get_hashed_manifest(), dict)
    assert isinstance(registry.get_agent_manifest(), dict)


def test_tool_event_handler_receives_execution_data():
    events: list[dict[str, object]] = []
    registry = ActionRegistry(tool_event_handler=events.append)
    skill = LoadedSkill(
        name="utility",
        description="utility tools",
        tools=[
            LoadedTool(
                name="echo_tool",
                func=_dummy_tool,
                schema={
                    "name": "echo_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            )
        ],
    )
    registry.load_skills({"utility": skill})

    tool = registry.eap_registry._tools["echo_tool"]
    result = tool(value="hello")

    assert result == {"ok": {"value": "hello"}}
    assert events
    event = events[-1]
    assert event["event"] == "tool_execution"
    assert event["tool_name"] == "echo_tool"
    assert event["outcome"] == "success"
