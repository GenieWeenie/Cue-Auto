"""Filesystem watcher for hot-reloading skills."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Callback signature: (path, event_type) where event_type is "created", "modified", or "deleted"
OnChangeCallback = Callable[[Path, str], Awaitable[None]]


class SkillWatcher:
    """Polls the skills directory for changes and triggers reload callbacks."""

    def __init__(self, skills_dir: str, on_change: OnChangeCallback):
        self._dir = Path(skills_dir)
        self._on_change = on_change
        self._mtimes: dict[str, float] = {}
        self._running = False

    async def start(self, poll_interval: float = 2.0) -> None:
        """Poll the skills directory for changes."""
        self._running = True
        logger.info("Skill watcher started (dir=%s, interval=%.1fs)", self._dir, poll_interval)

        # Build initial snapshot
        self._mtimes = self._scan()

        while self._running:
            await asyncio.sleep(poll_interval)
            try:
                await self._check_changes()
            except Exception:
                logger.exception("Skill watcher error during check")

    def stop(self) -> None:
        self._running = False
        logger.info("Skill watcher stopped")

    def _scan(self) -> dict[str, float]:
        """Scan the skills directory and return {path_key: mtime} map."""
        if not self._dir.exists():
            return {}

        result: dict[str, float] = {}
        for item in self._dir.iterdir():
            if item.is_file() and item.suffix == ".py" and not item.name.startswith("_"):
                result[str(item)] = os.path.getmtime(item)
            elif item.is_dir():
                skill_file = item / "skill.py"
                if skill_file.exists():
                    # Track the max mtime of all files in the pack
                    pack_mtime = max(os.path.getmtime(f) for f in item.iterdir() if f.is_file())
                    result[str(item)] = pack_mtime
        return result

    async def _check_changes(self) -> None:
        """Compare current filesystem state to cached mtimes."""
        current = self._scan()

        # Check for new and modified files
        for key, mtime in current.items():
            if key not in self._mtimes:
                logger.info("New skill detected: %s", key)
                await self._safe_emit(Path(key), "created")
            elif _mtime_changed(mtime, self._mtimes[key]):
                logger.info("Skill modified: %s", key)
                await self._safe_emit(Path(key), "modified")

        # Check for deleted files
        for key in set(self._mtimes) - set(current):
            logger.info("Skill deleted: %s", key)
            await self._safe_emit(Path(key), "deleted")

        self._mtimes = current

    async def _safe_emit(self, path: Path, kind: str) -> None:
        """Emit a change event, isolating failures per-callback."""
        try:
            await self._on_change(path, kind)
        except Exception:
            logger.exception("Skill watcher callback failed", extra={"path": str(path), "event_type": kind})


def _mtime_changed(new: float, old: float) -> bool:
    """Compare mtimes with tolerance for float imprecision."""
    return abs(new - old) > 0.01
