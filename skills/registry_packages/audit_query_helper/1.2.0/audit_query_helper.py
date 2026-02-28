"""Community skill package: audit query helper."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "audit_query_helper",
    "description": "Build normalized audit query suggestions.",
    "tools": [
        {
            "name": "suggest_audit_filters",
            "schema": {
                "name": "suggest_audit_filters",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_type": {"type": "string"},
                        "risk_level": {"type": "string"},
                        "user_id": {"type": "string"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def suggest_audit_filters(event_type: str = "", risk_level: str = "", user_id: str = "") -> dict:
    filters: list[str] = []
    if event_type.strip():
        filters.append(f"event={event_type.strip()}")
    if risk_level.strip():
        filters.append(f"risk={risk_level.strip()}")
    if user_id.strip():
        filters.append(f"user={user_id.strip()}")
    return {"command": "/audit json " + " ".join(filters).strip(), "filters": filters}
