"""Reads SOUL.md and injects agent identity into system prompts."""

from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)


class SoulLoader:
    def __init__(self, soul_md_path: str = "SOUL.md"):
        self._path = soul_md_path
        self._content: str | None = None
        self._mtime: float = 0.0

    def load(self) -> str:
        """Read SOUL.md from disk, caching until the file changes."""
        try:
            current_mtime = os.path.getmtime(self._path)
        except OSError:
            logger.warning("SOUL.md not found at %s, using empty identity", self._path)
            return ""

        if self._content is None or current_mtime != self._mtime:
            with open(self._path, "r", encoding="utf-8") as f:
                self._content = f.read().strip()
            self._mtime = current_mtime
            logger.info("Loaded SOUL.md (%d chars)", len(self._content))

        return self._content

    def inject(self, base_prompt: str = "") -> str:
        """Prepend SOUL.md identity to a system prompt."""
        soul = self.load()
        if not soul:
            return base_prompt

        parts = [f"### IDENTITY ###\n{soul}"]
        if base_prompt:
            parts.append(f"### INSTRUCTIONS ###\n{base_prompt}")
        return "\n\n".join(parts)
