from __future__ import annotations

from pathlib import Path

import pytest

from cue_agent.skills.scaffold import create_skill_scaffold, normalize_skill_name


def test_normalize_skill_name():
    assert normalize_skill_name("Daily Brief") == "daily_brief"
    assert normalize_skill_name("123-alerts") == "skill_123_alerts"
    assert normalize_skill_name("Ops@Bot!") == "opsbot"


def test_create_pack_scaffold(tmp_path: Path):
    created = create_skill_scaffold("daily_brief", skills_dir=str(tmp_path), style="pack")
    assert created == tmp_path / "daily_brief"
    assert (created / "skill.py").exists()
    assert (created / "prompt.md").exists()
    assert (created / "config.yaml").exists()
    assert (created / "README.md").exists()


def test_create_simple_scaffold(tmp_path: Path):
    created = create_skill_scaffold("ops_note", skills_dir=str(tmp_path), style="simple")
    assert created == tmp_path / "ops_note.py"
    content = created.read_text(encoding="utf-8")
    assert "def run(input: str) -> dict:" in content


def test_create_skill_scaffold_refuses_overwrite(tmp_path: Path):
    create_skill_scaffold("daily_brief", skills_dir=str(tmp_path), style="pack")
    with pytest.raises(FileExistsError):
        create_skill_scaffold("daily_brief", skills_dir=str(tmp_path), style="pack")
