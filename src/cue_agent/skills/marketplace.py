"""Community skill registry search/install/update/validation helpers."""

from __future__ import annotations

import ast
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cue_agent.skills.testing import SkillTestHarness

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Semver:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Semver:
        raw = text.strip()
        if not _SEMVER_RE.match(raw):
            raise ValueError(f"Invalid semver: {text}")
        major, minor, patch = raw.split(".")
        return cls(major=int(major), minor=int(minor), patch=int(patch))

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def is_version_compatible(version: str, constraint: str) -> bool:
    version_semver = Semver.parse(version)
    text = constraint.strip()
    if not text or text == "*":
        return True
    for token in [part.strip() for part in text.split(",") if part.strip()]:
        if token.startswith(">="):
            if version_semver.as_tuple() < Semver.parse(token[2:].strip()).as_tuple():
                return False
            continue
        if token.startswith("<="):
            if version_semver.as_tuple() > Semver.parse(token[2:].strip()).as_tuple():
                return False
            continue
        if token.startswith(">"):
            if version_semver.as_tuple() <= Semver.parse(token[1:].strip()).as_tuple():
                return False
            continue
        if token.startswith("<"):
            if version_semver.as_tuple() >= Semver.parse(token[1:].strip()).as_tuple():
                return False
            continue
        if token.startswith("="):
            if version_semver.as_tuple() != Semver.parse(token[1:].strip()).as_tuple():
                return False
            continue
        if version_semver.as_tuple() != Semver.parse(token).as_tuple():
            return False
    return True


class SkillMarketplace:
    def __init__(
        self,
        *,
        index_path: str,
        packages_dir: str,
        install_dir: str,
        installed_state_path: str,
        cue_agent_version: str,
    ):
        self._index_path = Path(index_path).expanduser()
        self._packages_dir = Path(packages_dir).expanduser()
        self._install_dir = Path(install_dir).expanduser()
        self._installed_state_path = Path(installed_state_path).expanduser()
        self._cue_agent_version = cue_agent_version

    def search(self, query: str = "", *, limit: int = 10) -> list[dict[str, Any]]:
        terms = query.strip().lower()
        rows: list[dict[str, Any]] = []
        for skill in self._load_skills():
            text_blob = " ".join(
                [
                    str(skill.get("id", "")),
                    str(skill.get("name", "")),
                    str(skill.get("description", "")),
                    " ".join(str(tag) for tag in skill.get("tags", [])),
                ]
            ).lower()
            if terms and terms not in text_blob:
                continue
            latest = self._latest_version(skill)
            if latest is None:
                continue
            rows.append(
                {
                    "id": str(skill.get("id", "")),
                    "name": str(skill.get("name", "")),
                    "description": str(skill.get("description", "")),
                    "latest_version": str(latest.get("version", "")),
                    "cue_agent_constraint": str(latest.get("cue_agent_constraint", "*")),
                    "quality_score": float(latest.get("quality_score", 0.0)),
                    "success_rate": float(latest.get("success_rate", 0.0)),
                    "usage_count": int(latest.get("usage_count", 0)),
                    "rating_average": float(skill.get("rating_average", 0.0)),
                    "rating_count": int(skill.get("rating_count", 0)),
                    "tags": [str(tag) for tag in skill.get("tags", [])],
                }
            )
        rows.sort(key=lambda row: (row["quality_score"], row["usage_count"]), reverse=True)
        return rows[: max(1, limit)]

    def install(self, skill_id: str, *, version: str | None = None, force: bool = False) -> dict[str, Any]:
        skill = self._skill_by_id(skill_id)
        selected = self._select_version(skill, version=version)
        selected_version = str(selected.get("version", ""))
        constraint = str(selected.get("cue_agent_constraint", "*"))
        if not is_version_compatible(self._cue_agent_version, constraint):
            raise ValueError(
                f"Skill {skill_id}@{selected_version} requires CueAgent {constraint}, "
                f"current is {self._cue_agent_version}"
            )
        source = self._resolve_source(selected)
        report = self.validate_submission(source)
        if not report["ok"]:
            raise ValueError(f"Submission validation failed: {'; '.join(report['errors'])}")

        destination = self._destination_path(skill_id=skill_id, source=source)
        self._copy_source(source, destination, force=force)

        state = self._load_installed_state()
        state[skill_id] = {
            "skill_id": skill_id,
            "version": selected_version,
            "path": str(destination),
            "installed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._save_installed_state(state)
        return {
            "skill_id": skill_id,
            "version": selected_version,
            "path": str(destination),
            "validation": report,
            "hot_reload_hint": "Skill files were written to skills directory and are eligible for hot-reload.",
        }

    def update(self, skill_id: str) -> dict[str, Any]:
        state = self._load_installed_state()
        current = state.get(skill_id)
        if current is None:
            raise ValueError(f"Skill {skill_id} is not currently installed")
        current_version = str(current.get("version", "0.0.0"))
        skill = self._skill_by_id(skill_id)
        latest = self._latest_compatible_version(skill, self._cue_agent_version)
        if latest is None:
            raise ValueError(f"No compatible versions available for {skill_id}")
        latest_version = str(latest.get("version", ""))
        if Semver.parse(latest_version).as_tuple() <= Semver.parse(current_version).as_tuple():
            return {
                "skill_id": skill_id,
                "status": "up_to_date",
                "version": current_version,
            }
        installed = self.install(skill_id, version=latest_version, force=True)
        installed["status"] = "updated"
        installed["previous_version"] = current_version
        return installed

    def update_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for skill_id in sorted(self._load_installed_state().keys()):
            try:
                results.append(self.update(skill_id))
            except Exception as exc:
                results.append({"skill_id": skill_id, "status": "error", "error": str(exc)})
        return results

    def validate_registry_index(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        skills = self._load_skills()
        for idx, skill in enumerate(skills):
            prefix = f"skills[{idx}]"
            skill_id = str(skill.get("id", "")).strip()
            if not skill_id:
                errors.append(f"{prefix}.id is required")
            if not str(skill.get("name", "")).strip():
                errors.append(f"{prefix}.name is required")
            if not str(skill.get("description", "")).strip():
                errors.append(f"{prefix}.description is required")
            tags = skill.get("tags", [])
            if not isinstance(tags, list):
                errors.append(f"{prefix}.tags must be an array")
            versions = skill.get("versions", [])
            if not isinstance(versions, list) or not versions:
                errors.append(f"{prefix}.versions must contain at least one version")
                continue
            for vidx, version_info in enumerate(versions):
                vp = f"{prefix}.versions[{vidx}]"
                version_text = str(version_info.get("version", "")).strip()
                if not version_text or not _SEMVER_RE.match(version_text):
                    errors.append(f"{vp}.version must be semver")
                cue_constraint = str(version_info.get("cue_agent_constraint", "")).strip()
                if not cue_constraint:
                    errors.append(f"{vp}.cue_agent_constraint is required")
                package_path = version_info.get("package_path")
                if not isinstance(package_path, str) or not package_path.strip():
                    errors.append(f"{vp}.package_path is required")
                    continue
                source = (self._packages_dir / package_path).expanduser()
                if not source.exists():
                    errors.append(f"{vp}.package_path does not exist: {source}")
                    continue
                for key in ("usage_count", "quality_score", "success_rate", "security_reviewed", "docs_url"):
                    if key not in version_info:
                        errors.append(f"{vp}.{key} is required")
                report = self.validate_submission(source)
                if not report["ok"]:
                    errors.extend([f"{vp}: {message}" for message in report["errors"]])
                warnings.extend([f"{vp}: {message}" for message in report["warnings"]])
        return {"ok": not errors, "errors": errors, "warnings": warnings, "skill_count": len(skills)}

    def validate_submission(self, source_path: str | Path) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        source = Path(source_path).expanduser()
        module_file: Path | None = None
        docs_ok = False

        if source.is_file() and source.suffix == ".py":
            module_file = source
            docs_ok = _has_module_docstring(module_file)
            if not docs_ok:
                warnings.append("Simple skill should include a module-level docstring for documentation quality")
        elif source.is_dir() and (source / "skill.py").exists():
            module_file = source / "skill.py"
            docs_ok = (source / "README.md").exists() or (source / "prompt.md").exists()
            if not docs_ok:
                errors.append("Skill pack must include README.md or prompt.md")
        else:
            errors.append("Submission path must be a .py file or a directory containing skill.py")

        if module_file is None:
            return {"ok": False, "errors": errors, "warnings": warnings}

        code = module_file.read_text(encoding="utf-8")
        insecure_patterns = [
            "os.system(",
            "subprocess.Popen(",
            "subprocess.call(",
            "eval(",
            "exec(",
            "pickle.loads(",
        ]
        for pattern in insecure_patterns:
            if pattern in code:
                errors.append(f"Security policy violation: found unsafe pattern `{pattern}`")

        try:
            harness = SkillTestHarness.from_path(module_file)
            manifest = harness.manifest()
        except Exception as exc:
            errors.append(f"Failed to load skill module: {exc}")
            return {"ok": False, "errors": errors, "warnings": warnings}

        if not str(manifest.get("name", "")).strip():
            errors.append("SKILL_MANIFEST.name is required")
        if not str(manifest.get("description", "")).strip():
            errors.append("SKILL_MANIFEST.description is required")
        tools = manifest.get("tools", [])
        if not isinstance(tools, list) or not tools:
            errors.append("SKILL_MANIFEST.tools must include at least one tool")
        else:
            for idx, tool in enumerate(tools):
                if not isinstance(tool, dict):
                    errors.append(f"tool[{idx}] must be an object")
                    continue
                tool_name = str(tool.get("name", "")).strip()
                if not tool_name:
                    errors.append(f"tool[{idx}].name is required")
                    continue
                if "schema" not in tool:
                    errors.append(f"tool[{idx}].schema is required")
                else:
                    try:
                        harness.get_tool(tool_name)
                    except KeyError:
                        errors.append(f"Tool function '{tool_name}' is missing")

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "docs_ok": docs_ok,
            "skill_name": str(manifest.get("name", "")),
        }

    def _load_skills(self) -> list[dict[str, Any]]:
        if not self._index_path.exists():
            raise FileNotFoundError(f"Registry index not found: {self._index_path}")
        payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        skills = payload.get("skills", []) if isinstance(payload, dict) else []
        if not isinstance(skills, list):
            raise ValueError("Registry index must contain a top-level 'skills' array")
        output: list[dict[str, Any]] = []
        for item in skills:
            if isinstance(item, dict):
                output.append(item)
        return output

    def _skill_by_id(self, skill_id: str) -> dict[str, Any]:
        target = skill_id.strip().lower()
        for skill in self._load_skills():
            if str(skill.get("id", "")).strip().lower() == target:
                return skill
        raise ValueError(f"Skill '{skill_id}' not found in registry")

    @staticmethod
    def _latest_version(skill: dict[str, Any]) -> dict[str, Any] | None:
        versions = skill.get("versions", [])
        if not isinstance(versions, list):
            return None
        candidates = [version for version in versions if isinstance(version, dict) and "version" in version]
        if not candidates:
            return None
        candidates.sort(key=lambda item: Semver.parse(str(item.get("version", "0.0.0"))).as_tuple(), reverse=True)
        return candidates[0]

    @staticmethod
    def _select_version(skill: dict[str, Any], *, version: str | None) -> dict[str, Any]:
        versions = skill.get("versions", [])
        if not isinstance(versions, list):
            raise ValueError("Skill has no versions")
        if version:
            for candidate in versions:
                if isinstance(candidate, dict) and str(candidate.get("version", "")) == version:
                    return candidate
            raise ValueError(f"Version {version} not found for skill {skill.get('id', '')}")
        latest = SkillMarketplace._latest_version(skill)
        if latest is None:
            raise ValueError(f"Skill {skill.get('id', '')} has no versions")
        return latest

    @staticmethod
    def _latest_compatible_version(skill: dict[str, Any], cue_version: str) -> dict[str, Any] | None:
        versions = skill.get("versions", [])
        if not isinstance(versions, list):
            return None
        compatible = []
        for candidate in versions:
            if not isinstance(candidate, dict):
                continue
            constraint = str(candidate.get("cue_agent_constraint", "*"))
            version_text = str(candidate.get("version", "0.0.0"))
            if not _SEMVER_RE.match(version_text):
                continue
            if is_version_compatible(cue_version, constraint):
                compatible.append(candidate)
        if not compatible:
            return None
        compatible.sort(key=lambda item: Semver.parse(str(item.get("version", "0.0.0"))).as_tuple(), reverse=True)
        return compatible[0]

    def _resolve_source(self, version_info: dict[str, Any]) -> Path:
        package_path = str(version_info.get("package_path", "")).strip()
        if not package_path:
            raise ValueError("Version record is missing package_path")
        source = (self._packages_dir / package_path).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"Package source not found: {source}")
        return source

    def _destination_path(self, *, skill_id: str, source: Path) -> Path:
        self._install_dir.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            return self._install_dir / f"{skill_id}.py"
        return self._install_dir / skill_id

    @staticmethod
    def _copy_source(source: Path, destination: Path, *, force: bool) -> None:
        if destination.exists():
            if not force:
                raise FileExistsError(f"{destination} already exists (use force to overwrite)")
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()

        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)

    def _load_installed_state(self) -> dict[str, dict[str, str]]:
        if not self._installed_state_path.exists():
            return {}
        payload = json.loads(self._installed_state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        installed = payload.get("installed", {})
        if not isinstance(installed, dict):
            return {}
        output: dict[str, dict[str, str]] = {}
        for skill_id, row in installed.items():
            if isinstance(row, dict):
                output[str(skill_id)] = {
                    "skill_id": str(row.get("skill_id", skill_id)),
                    "version": str(row.get("version", "")),
                    "path": str(row.get("path", "")),
                    "installed_at_utc": str(row.get("installed_at_utc", "")),
                }
        return output

    def _save_installed_state(self, state: dict[str, dict[str, str]]) -> None:
        self._installed_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "installed": state,
        }
        self._installed_state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _has_module_docstring(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(ast.get_docstring(tree))
