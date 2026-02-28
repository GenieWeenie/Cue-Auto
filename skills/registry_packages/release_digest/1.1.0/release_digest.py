"""Community skill package: release digest builder (v1.1.0)."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "release_digest",
    "description": "Generate release digests with highlights and risk tags.",
    "tools": [
        {
            "name": "build_release_digest",
            "schema": {
                "name": "build_release_digest",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "string", "description": "Release version"},
                        "changes": {
                            "type": "array",
                            "description": "List of release changes",
                            "items": {"type": "string"},
                        },
                        "known_risks": {
                            "type": "array",
                            "description": "Known release risks",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["version", "changes"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def build_release_digest(version: str, changes: list[str], known_risks: list[str] | None = None) -> dict:
    bullets = [item.strip() for item in changes if item.strip()]
    risks = [item.strip() for item in (known_risks or []) if item.strip()]
    return {
        "version": version,
        "summary": bullets[:5],
        "change_count": len(bullets),
        "risk_highlights": risks[:3],
    }
