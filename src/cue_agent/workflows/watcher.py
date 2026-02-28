"""Filesystem watcher for workflow YAML hot-reload."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from cue_agent.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)

OnReloadCallback = Callable[[], Awaitable[None]]


class WorkflowWatcher:
    """Poll workflow directory and trigger reload callback on any definition changes."""

    def __init__(self, workflows_dir: str, on_reload: OnReloadCallback):
        self._loader = WorkflowLoader(workflows_dir)
        self._on_reload = on_reload
        self._fingerprint: dict[str, float] = {}
        self._running = False

    async def start(self, poll_interval: float = 2.0) -> None:
        self._running = True
        self._fingerprint = self._loader.fingerprint()
        logger.info("Workflow watcher started (dir=%s, interval=%.1fs)", self._loader.workflows_dir, poll_interval)
        while self._running:
            await asyncio.sleep(poll_interval)
            try:
                await self._check_changes()
            except Exception:
                logger.exception("Workflow watcher failed during change detection")

    def stop(self) -> None:
        self._running = False
        logger.info("Workflow watcher stopped")

    async def _check_changes(self) -> None:
        current = self._loader.fingerprint()
        if current == self._fingerprint:
            return
        before = set(self._fingerprint.keys())
        after = set(current.keys())
        created = after - before
        deleted = before - after
        modified = {key for key in before & after if current[key] != self._fingerprint[key]}
        self._fingerprint = current
        logger.info(
            "Workflow definitions changed",
            extra={
                "event": "workflow_definitions_changed",
                "workflow_created_paths": sorted(created),
                "workflow_deleted_paths": sorted(deleted),
                "workflow_modified_paths": sorted(modified),
            },
        )
        await self._on_reload()

    @property
    def workflows_dir(self) -> Path:
        return self._loader.workflows_dir
