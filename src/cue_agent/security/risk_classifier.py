"""Classifies tool calls by context-aware risk level."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from string import hexdigits
from typing import Any


RiskLevel = str
_RISK_ORDER: tuple[RiskLevel, ...] = ("low", "medium", "high", "critical")
_DESTRUCTIVE_INTENT_TERMS = ("delete", "destroy", "wipe", "exfiltrate", "shutdown", "disable")
_SAFE_SHELL_PREFIXES = ("ls", "cat", "pwd", "echo", "date", "whoami", "uname", "head", "tail", "grep", "find")
_DEFAULT_RUN_SHELL_CRITICAL = (
    "rm -rf /",
    "mkfs",
    "shutdown",
    "reboot",
    "chmod 777 /",
    "curl ",
    "| sh",
    "dd if=",
)
_DEFAULT_RUN_SHELL_HIGH = (
    "rm -rf",
    "sudo ",
    "chmod ",
    "chown ",
    "systemctl ",
    "launchctl ",
    "git reset --hard",
    "docker ",
)
_DEFAULT_RUN_SHELL_MEDIUM = (
    "pip install",
    "npm install",
    "brew install",
    "apt-get",
    "git ",
)
_DEFAULT_WRITE_CRITICAL_PATH_TOKENS = ("/etc/", "/usr/bin/", "/bin/", "/sbin/", ".ssh", ".env")
_DEFAULT_WRITE_HIGH_PATH_TOKENS = ("/var/", "/usr/local/", ".bashrc", ".zshrc", ".profile")
_DEFAULT_WRITE_MEDIUM_EXTENSIONS = (".sh", ".zsh", ".bash", ".ps1", ".service", ".yaml", ".yml")


@dataclass(frozen=True)
class RiskDecision:
    tool_name: str
    level: RiskLevel
    reason: str
    requires_approval: bool
    sandbox_dry_run: bool


class RiskClassifier:
    """Context-aware risk classification for tool calls."""

    def __init__(
        self,
        high_risk_tools: list[str],
        *,
        approval_required_levels: list[str] | None = None,
        rules_path: str | None = None,
        sandbox_dry_run: bool = False,
        workspace_root: str | None = None,
    ):
        self._high_risk = set(high_risk_tools)
        self._approval_required_levels = set(approval_required_levels or ["high", "critical"])
        self._rules_path = Path(rules_path).expanduser() if rules_path else None
        self._rules_mtime: float | None = None
        self._rules: dict[str, Any] = {}
        self._sandbox_dry_run = sandbox_dry_run
        root = Path(workspace_root) if workspace_root else Path.cwd()
        self._workspace_root = root.resolve()
        self._reload_rules_if_needed(force=True)

    @property
    def sandbox_dry_run(self) -> bool:
        return self._sandbox_dry_run

    def classify(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        intent: str = "",
        execution_context: dict[str, Any] | None = None,
    ) -> RiskLevel:
        """Return risk level (`low|medium|high|critical`) for a tool call."""
        return self.assess(
            tool_name,
            arguments=arguments,
            intent=intent,
            execution_context=execution_context,
        ).level

    def assess(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        intent: str = "",
        execution_context: dict[str, Any] | None = None,
    ) -> RiskDecision:
        self._reload_rules_if_needed()
        args = arguments or {}
        original = self._normalize_tool_name(tool_name)

        level = "low"
        reason = "default low-risk classification"
        if original == "run_shell":
            level, reason = self._classify_run_shell(args)
        elif original == "write_file":
            level, reason = self._classify_write_file(args)
        elif original in self._high_risk:
            level, reason = "high", "tool in configured high-risk list"

        level, reason = self._apply_contextual_adjustments(
            level=level,
            reason=reason,
            intent=intent,
            execution_context=execution_context or {},
        )

        requires_approval = self._requires_approval_level(level)
        if self._sandbox_dry_run and level != "low":
            requires_approval = True

        return RiskDecision(
            tool_name=original,
            level=level,
            reason=reason,
            requires_approval=requires_approval,
            sandbox_dry_run=self._sandbox_dry_run,
        )

    def is_high_risk(self, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        return self.classify(tool_name, arguments) in {"high", "critical"}

    def requires_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        intent: str = "",
        execution_context: dict[str, Any] | None = None,
    ) -> bool:
        return self.assess(
            tool_name,
            arguments=arguments,
            intent=intent,
            execution_context=execution_context,
        ).requires_approval

    def _normalize_tool_name(self, tool_name: str) -> str:
        # Strip any EAP hash suffix (e.g., "run_shell_abc12345" -> "run_shell")
        if "_" not in tool_name:
            return tool_name
        base, suffix = tool_name.rsplit("_", 1)
        clean_suffix = suffix.strip().lower()
        if len(clean_suffix) >= 6 and all(ch in hexdigits.lower() for ch in clean_suffix):
            return base
        return tool_name

    def _classify_run_shell(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str]:
        command = str(arguments.get("command") or arguments.get("cmd") or "").strip().lower()
        if not command:
            return "medium", "run_shell without command payload"

        critical_patterns = self._pattern_list("run_shell", "critical_patterns", _DEFAULT_RUN_SHELL_CRITICAL)
        high_patterns = self._pattern_list("run_shell", "high_patterns", _DEFAULT_RUN_SHELL_HIGH)
        medium_patterns = self._pattern_list("run_shell", "medium_patterns", _DEFAULT_RUN_SHELL_MEDIUM)
        if any(pattern in command for pattern in critical_patterns):
            return "critical", "destructive shell command pattern"
        if any(pattern in command for pattern in high_patterns):
            return "high", "high-impact shell command pattern"
        if any(pattern in command for pattern in medium_patterns):
            return "medium", "state-changing shell command pattern"
        if command.startswith(_SAFE_SHELL_PREFIXES):
            return "low", "read-only shell command pattern"
        return "medium", "unclassified shell command defaults to medium"

    def _classify_write_file(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str]:
        raw_path = str(arguments.get("path") or arguments.get("file") or "").strip()
        if not raw_path:
            return "medium", "write_file without target path"

        expanded = Path(raw_path).expanduser()
        candidate = (self._workspace_root / expanded) if not expanded.is_absolute() else expanded
        try:
            resolved = candidate.resolve(strict=False)
        except Exception:
            resolved = candidate
        resolved_str = str(resolved).lower()
        critical_tokens = self._pattern_list(
            "write_file",
            "critical_path_tokens",
            _DEFAULT_WRITE_CRITICAL_PATH_TOKENS,
        )
        high_tokens = self._pattern_list("write_file", "high_path_tokens", _DEFAULT_WRITE_HIGH_PATH_TOKENS)
        medium_exts = self._pattern_list("write_file", "medium_extensions", _DEFAULT_WRITE_MEDIUM_EXTENSIONS)
        within_workspace = self._is_within_workspace(resolved)

        if within_workspace:
            relative_critical_tokens = [token for token in critical_tokens if not token.startswith("/")]
            if any(token in resolved_str for token in relative_critical_tokens):
                return "critical", "write target matches critical workspace rule"
            if resolved.suffix.lower() in medium_exts:
                return "medium", "write target extension is medium-risk"
            return "low", "write target inside workspace non-sensitive path"

        if any(token in resolved_str for token in critical_tokens):
            return "critical", "write target matches critical path rule"
        if any(token in resolved_str for token in high_tokens):
            return "high", "write target matches high-risk path rule"
        if not within_workspace:
            return "high", "write target outside workspace"
        if resolved.suffix.lower() in medium_exts:
            return "medium", "write target extension is medium-risk"
        return "low", "write target default classification"

    def _apply_contextual_adjustments(
        self,
        *,
        level: RiskLevel,
        reason: str,
        intent: str,
        execution_context: dict[str, Any],
    ) -> tuple[RiskLevel, str]:
        adjusted = level
        adjusted_reason = reason
        intent_lc = intent.lower()
        if any(term in intent_lc for term in _DESTRUCTIVE_INTENT_TERMS):
            adjusted = self._elevate(adjusted, steps=1)
            adjusted_reason = f"{adjusted_reason}; elevated by risky intent"

        env = str(execution_context.get("environment") or "").strip().lower()
        if env in {"prod", "production"} and adjusted in {"medium", "high"}:
            adjusted = self._elevate(adjusted, steps=1)
            adjusted_reason = f"{adjusted_reason}; elevated for production context"
        return adjusted, adjusted_reason

    def _elevate(self, level: RiskLevel, *, steps: int) -> RiskLevel:
        try:
            idx = _RISK_ORDER.index(level)
        except ValueError:
            return "medium"
        next_idx = min(len(_RISK_ORDER) - 1, idx + max(0, steps))
        return _RISK_ORDER[next_idx]

    def _requires_approval_level(self, level: RiskLevel) -> bool:
        return level in self._approval_required_levels

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self._workspace_root)
            return True
        except ValueError:
            return False

    def _pattern_list(self, section: str, key: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
        scoped = self._rules.get(section)
        if not isinstance(scoped, dict):
            return defaults
        raw = scoped.get(key)
        if not isinstance(raw, list):
            return defaults
        values = [str(item).strip().lower() for item in raw if str(item).strip()]
        if not values:
            return defaults
        merged = tuple(dict.fromkeys([*defaults, *values]))
        return merged

    def _reload_rules_if_needed(self, *, force: bool = False) -> None:
        if self._rules_path is None:
            return
        if not self._rules_path.exists():
            self._rules = {}
            self._rules_mtime = None
            return

        stat = self._rules_path.stat()
        mtime = stat.st_mtime
        if not force and self._rules_mtime == mtime:
            return

        try:
            parsed = json.loads(self._rules_path.read_text(encoding="utf-8"))
        except Exception:
            self._rules = {}
            self._rules_mtime = mtime
            return
        if isinstance(parsed, dict):
            self._rules = parsed
            self._apply_rule_overrides(parsed)
        else:
            self._rules = {}
        self._rules_mtime = mtime

    def _apply_rule_overrides(self, parsed: dict[str, Any]) -> None:
        raw_levels = parsed.get("approval_required_levels")
        if not isinstance(raw_levels, list):
            return
        levels = [str(item).strip().lower() for item in raw_levels if str(item).strip()]
        valid = [level for level in levels if level in _RISK_ORDER]
        if valid:
            self._approval_required_levels = set(valid)
