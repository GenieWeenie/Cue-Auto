"""Tests for risk classification."""

from cue_agent.security.risk_classifier import RiskClassifier


def test_high_risk():
    classifier = RiskClassifier(["run_shell", "write_file"])
    assert classifier.classify("run_shell") == "high"
    assert classifier.is_high_risk("run_shell") is True


def test_low_risk():
    classifier = RiskClassifier(["run_shell"])
    assert classifier.classify("read_file") == "low"
    assert classifier.is_high_risk("read_file") is False


def test_hashed_name_stripped():
    classifier = RiskClassifier(["run_shell"])
    # EAP hashes look like "run_shell_abc12345"
    assert classifier.is_high_risk("run_shell_abc12345") is True
