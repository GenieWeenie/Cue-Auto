"""Example skill: simple greeting tool.

Drop .py files like this into the skills/ folder and CueAgent will auto-discover them.
Each skill needs a SKILL_MANIFEST dict and matching function implementations.
"""

SKILL_MANIFEST = {
    "name": "example_hello",
    "description": "A simple example skill that greets a user by name",
    "tools": [
        {
            "name": "say_hello",
            "schema": {
                "name": "say_hello",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the person to greet",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def say_hello(name: str) -> dict:
    """Greet someone by name."""
    return {"greeting": f"Hello, {name}! I'm CueAgent.", "status": "ok"}
