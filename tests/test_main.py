"""Tests for CLI entrypoint behavior."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
import json

import pytest

from cue_agent import __main__ as main_module


@dataclass
class _FakeReport:
    exit_code: int

    def to_text(self) -> str:
        return "fake diagnostics"


def test_main_check_config_exits_with_report_code(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cue-agent", "--check-config"])
    monkeypatch.setattr(
        "cue_agent.config_diagnostics.run_config_diagnostics",
        lambda config: _FakeReport(exit_code=0),
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "fake diagnostics" in output


def test_main_check_config_exits_1_when_diagnostics_fail(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cue-agent", "--check-config"])
    monkeypatch.setattr(
        "cue_agent.config_diagnostics.run_config_diagnostics",
        lambda config: _FakeReport(exit_code=1),
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1
    assert "fake diagnostics" in capsys.readouterr().out


def test_main_export_audit_to_stdout(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setenv("CUE_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(sys, "argv", ["cue-agent", "--export-audit-format", "json", "--audit-limit", "5"])

    main_module.main()

    output = capsys.readouterr().out
    assert "generated_at_utc" in output
    assert "exported" in output.lower()


def test_main_export_audit_to_file(monkeypatch, capsys, tmp_path: Path):
    output_path = tmp_path / "audit.md"
    monkeypatch.setenv("CUE_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cue-agent",
            "--export-audit-format",
            "markdown",
            "--audit-output",
            str(output_path),
        ],
    )

    main_module.main()

    assert output_path.exists()
    assert "CueAgent Audit Export" in output_path.read_text(encoding="utf-8")
    assert "Exported" in capsys.readouterr().out


def test_main_create_skill_pack(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cue-agent",
            "create-skill",
            "daily_brief",
            "--skills-dir",
            str(tmp_path),
        ],
    )

    main_module.main()

    assert (tmp_path / "daily_brief" / "skill.py").exists()
    assert "Created skill scaffold" in capsys.readouterr().out


def test_main_create_skill_simple(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cue-agent",
            "create-skill",
            "ops-note",
            "--skills-dir",
            str(tmp_path),
            "--style",
            "simple",
        ],
    )

    main_module.main()

    assert (tmp_path / "ops_note.py").exists()


def _write_marketplace_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    packages_dir = tmp_path / "registry_packages"
    package_file = packages_dir / "demo_skill" / "1.0.0" / "demo_skill.py"
    package_file.parent.mkdir(parents=True, exist_ok=True)
    package_file.write_text(
        """
SKILL_MANIFEST = {
    "name": "demo_skill",
    "description": "demo",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}],
}

def run() -> dict:
    return {"ok": True}
""",
        encoding="utf-8",
    )
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "id": "demo_skill",
                        "name": "Demo Skill",
                        "description": "test",
                        "tags": ["demo"],
                        "rating_average": 4.0,
                        "rating_count": 1,
                        "versions": [
                            {
                                "version": "1.0.0",
                                "cue_agent_constraint": ">=0.1.0,<0.3.0",
                                "package_path": "demo_skill/1.0.0/demo_skill.py",
                                "usage_count": 10,
                                "quality_score": 0.9,
                                "success_rate": 1.0,
                                "security_reviewed": True,
                                "docs_url": "https://example.test/demo",
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "installed.json"
    return index_path, packages_dir, skills_dir, state_path


def test_main_marketplace_search(monkeypatch, capsys, tmp_path: Path):
    index_path, packages_dir, skills_dir, state_path = _write_marketplace_fixture(tmp_path)
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_INDEX_PATH", str(index_path))
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_PACKAGES_DIR", str(packages_dir))
    monkeypatch.setenv("CUE_SKILLS_DIR", str(skills_dir))
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_STATE_PATH", str(state_path))
    monkeypatch.setattr(sys, "argv", ["cue-agent", "marketplace", "search", "demo"])

    main_module.main()

    output = capsys.readouterr().out
    assert "demo_skill@1.0.0" in output


def test_main_marketplace_search_empty(monkeypatch, capsys, tmp_path: Path):
    """Search with no matches prints message and returns."""
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"skills": []}, indent=2), encoding="utf-8")
    packages_dir = tmp_path / "packages"
    packages_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_INDEX_PATH", str(index_path))
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_PACKAGES_DIR", str(packages_dir))
    monkeypatch.setenv("CUE_SKILLS_DIR", str(skills_dir))
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_STATE_PATH", str(state_path))
    monkeypatch.setattr(sys, "argv", ["cue-agent", "marketplace", "search", "nonexistent"])

    main_module.main()

    assert "No marketplace skills found" in capsys.readouterr().out


def test_main_marketplace_install(monkeypatch, capsys, tmp_path: Path):
    index_path, packages_dir, skills_dir, state_path = _write_marketplace_fixture(tmp_path)
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_INDEX_PATH", str(index_path))
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_PACKAGES_DIR", str(packages_dir))
    monkeypatch.setenv("CUE_SKILLS_DIR", str(skills_dir))
    monkeypatch.setenv("CUE_SKILLS_REGISTRY_STATE_PATH", str(state_path))
    monkeypatch.setattr(sys, "argv", ["cue-agent", "marketplace", "install", "demo_skill"])

    main_module.main()

    assert (skills_dir / "demo_skill.py").exists()
    output = capsys.readouterr().out
    assert "Installed demo_skill@1.0.0" in output
