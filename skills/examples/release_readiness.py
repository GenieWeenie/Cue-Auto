"""Example skill: evaluate release readiness from checklist inputs."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "release_readiness",
    "description": "Score release readiness and return blocking checks.",
    "tools": [
        {
            "name": "evaluate_release",
            "schema": {
                "name": "evaluate_release",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "string", "description": "Release version"},
                        "quality_gates_passed": {"type": "boolean", "description": "CI quality gates status"},
                        "migration_notes_ready": {"type": "boolean", "description": "Migration notes status"},
                        "rollback_plan_ready": {"type": "boolean", "description": "Rollback readiness"},
                    },
                    "required": [
                        "version",
                        "quality_gates_passed",
                        "migration_notes_ready",
                        "rollback_plan_ready",
                    ],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def evaluate_release(
    version: str,
    quality_gates_passed: bool,
    migration_notes_ready: bool,
    rollback_plan_ready: bool,
) -> dict:
    checks = {
        "quality_gates_passed": quality_gates_passed,
        "migration_notes_ready": migration_notes_ready,
        "rollback_plan_ready": rollback_plan_ready,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    score = round((len(checks) - len(blockers)) / len(checks) * 100)
    return {
        "version": version,
        "score_percent": score,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
    }
