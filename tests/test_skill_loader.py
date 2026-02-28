"""Tests for the skill loading system."""

import tempfile
from pathlib import Path

import pytest

from cue_agent.skills.loader import SkillLoader


def _write_simple_skill(dir_path: Path, name: str = "test_skill") -> Path:
    """Write a simple skill .py file and return its path."""
    skill_file = dir_path / f"{name}.py"
    skill_file.write_text(
        f'''
SKILL_MANIFEST = {{
    "name": "{name}",
    "description": "A test skill",
    "tools": [
        {{
            "name": "do_thing",
            "schema": {{
                "name": "do_thing",
                "parameters": {{
                    "type": "object",
                    "properties": {{
                        "input": {{"type": "string", "description": "Input value"}}
                    }},
                    "required": ["input"],
                    "additionalProperties": False,
                }}
            }}
        }}
    ]
}}

def do_thing(input: str) -> dict:
    return {{"result": input.upper()}}
''',
        encoding="utf-8",
    )
    return skill_file


def _write_skill_pack(dir_path: Path, name: str = "test_pack") -> Path:
    """Write a skill pack folder and return its path."""
    pack_dir = dir_path / name
    pack_dir.mkdir()
    (pack_dir / "skill.py").write_text(
        f'''
SKILL_MANIFEST = {{
    "name": "{name}",
    "description": "A test skill pack",
    "tools": [
        {{
            "name": "pack_action",
            "schema": {{
                "name": "pack_action",
                "parameters": {{
                    "type": "object",
                    "properties": {{
                        "value": {{"type": "string", "description": "A value"}}
                    }},
                    "required": ["value"],
                    "additionalProperties": False,
                }}
            }}
        }}
    ]
}}

def pack_action(value: str) -> dict:
    return {{"processed": value}}
''',
        encoding="utf-8",
    )
    (pack_dir / "prompt.md").write_text("You are a test skill pack agent.", encoding="utf-8")
    (pack_dir / "config.yaml").write_text("api_key: test-key-123\ntimeout: 30\n", encoding="utf-8")
    return pack_dir


def test_discover_empty_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        loader = SkillLoader(tmpdir)
        assert loader.discover() == []


def test_discover_simple_skill():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_simple_skill(Path(tmpdir))
        loader = SkillLoader(tmpdir)
        paths = loader.discover()
        assert len(paths) == 1
        assert paths[0].suffix == ".py"


def test_discover_skill_pack():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_skill_pack(Path(tmpdir))
        loader = SkillLoader(tmpdir)
        paths = loader.discover()
        assert len(paths) == 1
        assert paths[0].is_dir()


def test_discover_both():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_simple_skill(Path(tmpdir), "simple_one")
        _write_skill_pack(Path(tmpdir), "pack_one")
        loader = SkillLoader(tmpdir)
        paths = loader.discover()
        assert len(paths) == 2


def test_load_simple_skill():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_simple_skill(Path(tmpdir))
        loader = SkillLoader(tmpdir)
        skills = loader.load_all()
        assert "test_skill" in skills
        skill = skills["test_skill"]
        assert skill.description == "A test skill"
        assert len(skill.tools) == 1
        assert skill.tools[0].name == "do_thing"
        # Verify the function works
        result = skill.tools[0].func(input="hello")
        assert result == {"result": "HELLO"}


def test_load_skill_pack():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_skill_pack(Path(tmpdir))
        loader = SkillLoader(tmpdir)
        skills = loader.load_all()
        assert "test_pack" in skills
        skill = skills["test_pack"]
        assert skill.prompt == "You are a test skill pack agent."
        assert skill.config == {"api_key": "test-key-123", "timeout": "30"}
        assert len(skill.tools) == 1
        result = skill.tools[0].func(value="test")
        assert result == {"processed": "test"}


def test_load_nonexistent_dir():
    loader = SkillLoader("/nonexistent/path")
    skills = loader.load_all()
    assert skills == {}


def test_reload_skill():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_simple_skill(Path(tmpdir))
        loader = SkillLoader(tmpdir)
        loader.load_all()
        # Reload
        skill = loader.reload_skill(path)
        assert skill.name == "test_skill"


def test_unload_skill():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_simple_skill(Path(tmpdir))
        loader = SkillLoader(tmpdir)
        loader.load_all()
        assert "test_skill" in loader.loaded
        loader.unload_skill("test_skill")
        assert "test_skill" not in loader.loaded


def _write_skill_with_deps(dir_path: Path, name: str, depends_on: list[str]) -> Path:
    """Write a simple skill .py with optional depends_on in manifest."""
    deps_str = repr(depends_on)
    skill_file = dir_path / f"{name}.py"
    skill_file.write_text(
        f'''
SKILL_MANIFEST = {{
    "name": "{name}",
    "description": "Skill {name}",
    "depends_on": {deps_str},
    "tools": [
        {{
            "name": "run",
            "schema": {{
                "name": "run",
                "parameters": {{
                    "type": "object",
                    "properties": {{"x": {{"type": "string"}}}},
                    "required": ["x"],
                    "additionalProperties": False,
                }}
            }}
        }}
    ]
}}

def run(x: str) -> dict:
    return {{"skill": "{name}", "x": x}}
''',
        encoding="utf-8",
    )
    return skill_file


def test_load_order_respects_dependencies():
    """Skills are loaded so that dependencies appear before dependents."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_skill_with_deps(root, "base", [])
        _write_skill_with_deps(root, "mid", ["base"])
        _write_skill_with_deps(root, "top", ["mid"])
        loader = SkillLoader(root)
        skills = loader.load_all()
        order = list(skills.keys())
        assert order.index("base") < order.index("mid")
        assert order.index("mid") < order.index("top")
        assert skills["base"].depends_on == []
        assert skills["mid"].depends_on == ["base"]
        assert skills["top"].depends_on == ["mid"]


def test_circular_dependency_detected():
    """Circular skill dependency is detected and reported clearly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_skill_with_deps(root, "skill_a", ["skill_b"])
        _write_skill_with_deps(root, "skill_b", ["skill_a"])
        loader = SkillLoader(root)
        with pytest.raises(ValueError) as exc_info:
            loader.load_all()
        assert "Circular skill dependency" in str(exc_info.value)
        assert "skill_a" in str(exc_info.value)
        assert "skill_b" in str(exc_info.value)
