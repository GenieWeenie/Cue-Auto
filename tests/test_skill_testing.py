from __future__ import annotations

from pathlib import Path

import pytest

from cue_agent.skills.testing import MockSkillContext, SkillTestHarness


def test_skill_test_harness_runs_tool_with_mock_context(tmp_path: Path):
    skill_file = tmp_path / "my_skill.py"
    skill_file.write_text(
        """
SKILL_MANIFEST = {
    "name": "my_skill",
    "description": "test",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object"}}}],
}

def run(value: str, context=None) -> dict:
    return {"value": value, "user_id": context.user_id}
""",
        encoding="utf-8",
    )

    harness = SkillTestHarness.from_path(skill_file)
    assert harness.manifest()["name"] == "my_skill"
    assert harness.list_tools() == ["run"]

    result = harness.run_tool("run", value="ok")
    assert result == {"value": "ok", "user_id": "test-user"}


def test_skill_test_harness_accepts_custom_context(tmp_path: Path):
    skill_file = tmp_path / "my_skill.py"
    skill_file.write_text(
        """
SKILL_MANIFEST = {
    "name": "my_skill",
    "description": "test",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object"}}}],
}

def run(value: str, context=None) -> dict:
    return {"value": value, "chat_id": context.chat_id}
""",
        encoding="utf-8",
    )

    harness = SkillTestHarness.from_path(skill_file)
    context = MockSkillContext(chat_id="alerts-room")
    result = harness.run_tool("run", value="ok", context=context)
    assert result == {"value": "ok", "chat_id": "alerts-room"}


def test_skill_test_harness_runs_async_tool(tmp_path: Path):
    """run_tool should await async tool functions via asyncio.run."""
    skill_file = tmp_path / "async_skill.py"
    skill_file.write_text(
        """
SKILL_MANIFEST = {
    "name": "async_skill",
    "description": "test",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object"}}}],
}

async def run(value: str, context=None) -> dict:
    return {"value": value, "user_id": context.user_id, "async": True}
""",
        encoding="utf-8",
    )

    harness = SkillTestHarness.from_path(skill_file)
    result = harness.run_tool("run", value="async-ok")
    assert result == {"value": "async-ok", "user_id": "test-user", "async": True}


@pytest.mark.asyncio
async def test_skill_test_harness_run_tool_async_with_async_func(tmp_path: Path):
    """run_tool_async should await async tools inside an event loop."""
    skill_file = tmp_path / "async_skill2.py"
    skill_file.write_text(
        """
SKILL_MANIFEST = {
    "name": "async_skill2",
    "description": "test",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object"}}}],
}

async def run(value: str, context=None) -> dict:
    return {"value": value, "async": True}
""",
        encoding="utf-8",
    )

    harness = SkillTestHarness.from_path(skill_file)
    result = await harness.run_tool_async("run", value="hello")
    assert result == {"value": "hello", "async": True}


@pytest.mark.asyncio
async def test_skill_test_harness_run_tool_async_with_sync_func(tmp_path: Path):
    """run_tool_async should work with sync tools too."""
    skill_file = tmp_path / "sync_skill.py"
    skill_file.write_text(
        """
SKILL_MANIFEST = {
    "name": "sync_skill",
    "description": "test",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object"}}}],
}

def run(value: str, context=None) -> dict:
    return {"value": value, "sync": True}
""",
        encoding="utf-8",
    )

    harness = SkillTestHarness.from_path(skill_file)
    result = await harness.run_tool_async("run", value="world")
    assert result == {"value": "world", "sync": True}
