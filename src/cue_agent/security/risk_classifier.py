"""Classifies tool calls by risk level."""

from __future__ import annotations


class RiskClassifier:
    """Lookup-based risk classification for tool calls."""

    def __init__(self, high_risk_tools: list[str]):
        self._high_risk = set(high_risk_tools)

    def classify(self, tool_name: str) -> str:
        """Return 'high' or 'low' risk for a given tool name."""
        # Strip any EAP hash suffix (e.g., "run_shell_abc12345" -> "run_shell")
        base_name = tool_name.rsplit("_", 1)[0] if "_" in tool_name else tool_name
        if base_name in self._high_risk or tool_name in self._high_risk:
            return "high"
        return "low"

    def is_high_risk(self, tool_name: str) -> bool:
        return self.classify(tool_name) == "high"
