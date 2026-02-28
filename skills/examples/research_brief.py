"""Example skill: build a structured research brief."""

from __future__ import annotations


SKILL_MANIFEST = {
    "name": "research_brief",
    "description": "Generate a concise research brief with source links and open questions.",
    "tools": [
        {
            "name": "build_brief",
            "schema": {
                "name": "build_brief",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "Research topic"},
                        "findings": {
                            "type": "array",
                            "description": "List of key findings",
                            "items": {"type": "string"},
                        },
                        "sources": {
                            "type": "array",
                            "description": "Source URLs or citations",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["topic", "findings"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def build_brief(topic: str, findings: list[str], sources: list[str] | None = None) -> dict:
    cleaned_sources = [s.strip() for s in (sources or []) if s.strip()]
    return {
        "topic": topic,
        "summary": findings[:3],
        "full_findings": findings,
        "sources": cleaned_sources,
        "open_questions": [
            f"What changed most recently for '{topic}'?",
            f"Which source for '{topic}' is most authoritative?",
        ],
    }
