"""Example skill: format incident events into a timeline summary."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "incident_timeline",
    "description": "Create a normalized incident timeline and escalation flag.",
    "tools": [
        {
            "name": "build_timeline",
            "schema": {
                "name": "build_timeline",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "incident_id": {"type": "string", "description": "Incident identifier"},
                        "events": {
                            "type": "array",
                            "description": "Event entries as 'timestamp | detail'",
                            "items": {"type": "string"},
                        },
                        "severity": {"type": "string", "description": "Incident severity (sev1-sev4)"},
                    },
                    "required": ["incident_id", "events", "severity"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def build_timeline(incident_id: str, events: list[str], severity: str) -> dict:
    normalized = [entry.strip() for entry in events if entry.strip()]
    return {
        "incident_id": incident_id,
        "severity": severity.lower(),
        "event_count": len(normalized),
        "timeline": normalized,
        "escalate_now": severity.lower() in {"sev1", "sev2"},
    }
