"""Tests for CLI entrypoint behavior."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

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
