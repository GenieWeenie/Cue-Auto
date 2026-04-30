"""Tests for workflow filesystem watcher hot-reload behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cue_agent.workflows.watcher import WorkflowWatcher


@pytest.mark.asyncio
async def test_workflow_watcher_detects_change(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = workflows_dir / "demo.yaml"
    workflow_file.write_text("name: demo\nsteps:\n  - id: s1\n    type: llm\n", encoding="utf-8")
    reloads: list[int] = []

    async def _on_reload() -> None:
        reloads.append(1)

    watcher = WorkflowWatcher(str(workflows_dir), on_reload=_on_reload)
    watcher._fingerprint = watcher._loader.fingerprint()  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)  # ensure rewrite produces a distinguishable mtime
    workflow_file.write_text(
        "name: demo\nsteps:\n  - id: s1\n    type: llm\n  - id: s2\n    type: llm\n", encoding="utf-8"
    )
    await watcher._check_changes()  # type: ignore[attr-defined]
    assert reloads == [1]


@pytest.mark.asyncio
async def test_workflow_watcher_start_stop_loop(tmp_path: Path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "demo.yaml").write_text("name: demo\nsteps:\n  - id: s1\n    type: llm\n", encoding="utf-8")
    ticks: list[int] = []

    async def _on_reload() -> None:
        ticks.append(1)

    watcher = WorkflowWatcher(str(workflows_dir), on_reload=_on_reload)
    task = asyncio.create_task(watcher.start(poll_interval=0.01))
    await asyncio.sleep(0.03)
    watcher.stop()
    await task
