"""Tests for risk classification."""

from __future__ import annotations

import json

from cue_agent.security.risk_classifier import RiskClassifier


def test_high_risk_by_tool_list():
    classifier = RiskClassifier(["send_telegram"])
    assert classifier.classify("send_telegram") == "high"
    assert classifier.classify("read_file") == "low"


def test_low_risk():
    classifier = RiskClassifier(["run_shell"])
    assert classifier.classify("read_file") == "low"
    assert classifier.is_high_risk("read_file") is False


def test_hashed_name_stripped():
    classifier = RiskClassifier(["run_shell"])
    # EAP hashes look like "run_shell_abc12345"
    assert classifier.is_high_risk("run_shell_abc12345", {"command": "rm -rf /tmp/data"}) is True


def test_run_shell_context_sensitive_levels():
    classifier = RiskClassifier(["run_shell"])
    assert classifier.classify("run_shell", {"command": "ls -la"}) == "low"
    assert classifier.classify("run_shell", {"command": "git status"}) == "medium"
    assert classifier.classify("run_shell", {"command": "sudo systemctl restart nginx"}) == "high"
    assert classifier.classify("run_shell", {"command": "rm -rf /"}) == "critical"


def test_write_file_path_risk_by_target(tmp_path):
    classifier = RiskClassifier(["run_shell"], workspace_root=str(tmp_path))
    low_path = str(tmp_path / "notes.txt")
    high_path = "/var/log/app.log"
    critical_path = "/etc/passwd"

    assert classifier.classify("write_file", {"path": low_path}) == "low"
    assert classifier.classify("write_file", {"path": high_path}) == "high"
    assert classifier.classify("write_file", {"path": critical_path}) == "critical"


def test_approval_policy_by_risk_level():
    classifier = RiskClassifier(
        ["run_shell"],
        approval_required_levels=["medium", "high", "critical"],
    )
    assert classifier.requires_approval("run_shell", {"command": "git status"}) is True
    assert classifier.requires_approval("run_shell", {"command": "ls"}) is False


def test_rule_file_overrides_and_dry_run(tmp_path):
    rules_path = tmp_path / "risk_rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "approval_required_levels": ["critical"],
                "run_shell": {"critical_patterns": ["terraform destroy"]},
                "write_file": {"high_path_tokens": ["/opt/secure/"]},
            }
        ),
        encoding="utf-8",
    )
    classifier = RiskClassifier(
        ["run_shell"],
        rules_path=str(rules_path),
        sandbox_dry_run=True,
    )
    decision = classifier.assess("run_shell", {"command": "terraform destroy -auto-approve"})
    assert decision.level == "critical"
    assert decision.requires_approval is True
    assert decision.sandbox_dry_run is True
