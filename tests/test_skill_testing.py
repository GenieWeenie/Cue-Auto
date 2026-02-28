from __future__ import annotations

from pathlib import Path

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
