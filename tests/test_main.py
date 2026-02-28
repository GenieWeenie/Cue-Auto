"""Tests for CLI entrypoint behavior."""

from __future__ import annotations

import sys
from dataclasses import dataclass

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
