"""Community skill package: release digest builder (v1.0.0)."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "release_digest",
    "description": "Generate concise release digests from change bullets.",
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
                    },
                    "required": ["version", "changes"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def build_release_digest(version: str, changes: list[str]) -> dict:
    bullets = [item.strip() for item in changes if item.strip()]
    return {
        "version": version,
        "summary": bullets[:5],
        "change_count": len(bullets),
    }
