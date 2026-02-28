from __future__ import annotations

import json
from pathlib import Path

import pytest

from cue_agent.skills.marketplace import SkillMarketplace, is_version_compatible


def _write_package(path: Path, *, dangerous: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = """
SKILL_MANIFEST = {
    "name": "demo_skill",
    "description": "demo skill",
    "tools": [{"name": "run", "schema": {"name": "run", "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}],
}

def run() -> dict:
    return {"ok": True}
"""
    if dangerous:
        body += "\nos.system('echo bad')\n"
    path.write_text(body, encoding="utf-8")


def _make_marketplace(tmp_path: Path) -> tuple[SkillMarketplace, Path, Path, Path]:
    index_path = tmp_path / "registry" / "index.json"
    packages_dir = tmp_path / "packages"
    skills_dir = tmp_path / "skills"
    state_path = tmp_path / "installed.json"
    _write_package(packages_dir / "demo_skill" / "1.0.0" / "demo_skill.py")
    _write_package(packages_dir / "demo_skill" / "1.1.0" / "demo_skill.py")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "id": "demo_skill",
                        "name": "Demo Skill",
                        "description": "Test package",
                        "tags": ["demo"],
                        "rating_average": 4.8,
                        "rating_count": 10,
                        "versions": [
                            {
                                "version": "1.0.0",
                                "cue_agent_constraint": ">=0.1.0,<0.3.0",
                                "package_path": "demo_skill/1.0.0/demo_skill.py",
                                "usage_count": 10,
                                "quality_score": 0.9,
                                "success_rate": 0.95,
                                "security_reviewed": True,
                                "docs_url": "https://example.test/demo/1.0.0",
                            },
                            {
                                "version": "1.1.0",
                                "cue_agent_constraint": ">=0.1.0,<0.3.0",
                                "package_path": "demo_skill/1.1.0/demo_skill.py",
                                "usage_count": 20,
                                "quality_score": 0.96,
                                "success_rate": 0.98,
                                "security_reviewed": True,
                                "docs_url": "https://example.test/demo/1.1.0",
                            },
                        ],
                    }
                ]
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    market = SkillMarketplace(
        index_path=str(index_path),
        packages_dir=str(packages_dir),
        install_dir=str(skills_dir),
        installed_state_path=str(state_path),
        cue_agent_version="0.1.0",
    )
    return market, skills_dir, packages_dir, index_path


def test_marketplace_semver_compatibility():
    assert is_version_compatible("0.1.0", ">=0.1.0,<0.3.0") is True
    assert is_version_compatible("0.3.1", ">=0.1.0,<0.3.0") is False


def test_marketplace_search_and_install(tmp_path: Path):
    market, skills_dir, _packages_dir, _index_path = _make_marketplace(tmp_path)

    rows = market.search("demo")
    assert rows[0]["id"] == "demo_skill"
    assert rows[0]["latest_version"] == "1.1.0"

    result = market.install("demo_skill")
    assert result["version"] == "1.1.0"
    assert (skills_dir / "demo_skill.py").exists()


def test_marketplace_update_all(tmp_path: Path):
    market, skills_dir, _packages_dir, _index_path = _make_marketplace(tmp_path)
    market.install("demo_skill", version="1.0.0")

    rows = market.update_all()
    assert rows[0]["status"] == "updated"
    assert rows[0]["version"] == "1.1.0"
    assert (skills_dir / "demo_skill.py").exists()


def test_marketplace_validation_catches_security_pattern(tmp_path: Path):
    market, _skills_dir, packages_dir, _index_path = _make_marketplace(tmp_path)
    dangerous = packages_dir / "danger" / "1.0.0" / "danger.py"
    _write_package(dangerous, dangerous=True)

    report = market.validate_submission(dangerous)
    assert report["ok"] is False
    assert any("Security policy violation" in err for err in report["errors"])


def test_marketplace_registry_validation(tmp_path: Path):
    market, _skills_dir, _packages_dir, _index_path = _make_marketplace(tmp_path)
    report = market.validate_registry_index()
    assert report["ok"] is True
    assert report["skill_count"] == 1


def test_marketplace_rejects_incompatible_version(tmp_path: Path):
    market, _skills_dir, _packages_dir, index_path = _make_marketplace(tmp_path)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["skills"][0]["versions"][0]["cue_agent_constraint"] = ">=9.0.0"
    payload["skills"][0]["versions"][1]["cue_agent_constraint"] = ">=9.0.0"
    index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    with pytest.raises(ValueError):
        market.install("demo_skill")
