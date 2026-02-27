"""JSON schema constants for all built-in tools."""

SEND_TELEGRAM_SCHEMA = {
    "name": "send_telegram",
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "Telegram chat ID to send the message to"},
            "text": {"type": "string", "description": "Message text to send"},
        },
        "required": ["chat_id", "text"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

READ_FILE_SCHEMA = {
    "name": "read_file",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path to read"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write to"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

RUN_SHELL_SCHEMA = {
    "name": "run_shell",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30)",
                "minimum": 1,
                "maximum": 300,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}
