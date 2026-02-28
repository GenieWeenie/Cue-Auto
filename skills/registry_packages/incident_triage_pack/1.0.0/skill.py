"""Community skill pack: incident triage."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "incident_triage_pack",
    "description": "Normalize incident details and assign triage guidance.",
    "tools": [
        {
            "name": "triage_incident",
            "schema": {
                "name": "triage_incident",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "incident_id": {"type": "string"},
                        "severity": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["incident_id", "severity", "summary"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def triage_incident(incident_id: str, severity: str, summary: str) -> dict:
    level = severity.lower().strip()
    owner = "oncall-admin" if level in {"sev1", "sev2"} else "operator"
    return {
        "incident_id": incident_id,
        "severity": level,
        "owner_role": owner,
        "next_action": f"Review incident '{summary[:80]}' and post update",
    }
