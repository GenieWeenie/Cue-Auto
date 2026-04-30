"""Tests for skills directory filesystem watcher (hot-reload)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cue_agent.skills.watcher import SkillWatcher


@pytest.mark.asyncio
async def test_skill_watcher_scan_empty_dir(tmp_path: Path) -> None:
    """_scan on empty or missing dir returns {}."""
    watcher = SkillWatcher(str(tmp_path), on_change=_noop)
    assert watcher._scan() == {}

    missing = tmp_path / "nonexistent"
    watcher_missing = SkillWatcher(str(missing), on_change=_noop)
    assert watcher_missing._scan() == {}


@pytest.mark.asyncio
async def test_skill_watcher_scan_detects_py_files(tmp_path: Path) -> None:
    """_scan finds .py files and skill packs (dir with skill.py)."""
    (tmp_path / "foo.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "_private.py").write_text("y = 2", encoding="utf-8")
    pack = tmp_path / "mypack"
    pack.mkdir()
    (pack / "skill.py").write_text("def run(): pass", encoding="utf-8")

    watcher = SkillWatcher(str(tmp_path), on_change=_noop)
    mtimes = watcher._scan()
    assert str(tmp_path / "foo.py") in mtimes
    assert str(tmp_path / "_private.py") not in mtimes
    assert str(pack) in mtimes


@pytest.mark.asyncio
async def test_skill_watcher_check_changes_callback_created_modified_deleted(
    tmp_path: Path,
) -> None:
    """_check_changes invokes callback with created, modified, deleted."""
    events: list[tuple[Path, str]] = []

    async def on_change(path: Path, event_type: str) -> None:
        events.append((path, event_type))

    watcher = SkillWatcher(str(tmp_path), on_change=on_change)
    watcher._mtimes = watcher._scan()
    assert events == []

    (tmp_path / "new_skill.py").write_text("z = 3", encoding="utf-8")
    await watcher._check_changes()
    assert len(events) == 1
    assert events[-1][1] == "created"
    assert events[-1][0].name == "new_skill.py"

    await asyncio.sleep(0.05)  # ensure mtime differs with tolerance check
    (tmp_path / "new_skill.py").write_text("z = 4", encoding="utf-8")
    await watcher._check_changes()
    assert any(e[1] == "modified" for e in events)
    assert events[-1][0].name == "new_skill.py"

    (tmp_path / "new_skill.py").unlink()
    await watcher._check_changes()
    assert any(e[1] == "deleted" for e in events)


@pytest.mark.asyncio
async def test_skill_watcher_start_stop(tmp_path: Path) -> None:
    """start/stop loop runs and stops cleanly with short poll interval."""
    (tmp_path / "dummy.py").write_text("pass", encoding="utf-8")
    ticks: list[int] = []

    async def on_change(path: Path, event_type: str) -> None:
        del path, event_type
        ticks.append(1)

    watcher = SkillWatcher(str(tmp_path), on_change=on_change)
    task = asyncio.create_task(watcher.start(poll_interval=0.02))
    await asyncio.sleep(0.06)
    watcher.stop()
    await task
    assert not watcher._running


@pytest.mark.asyncio
async def test_skill_watcher_callback_failure_isolated_per_event(
    tmp_path: Path,
) -> None:
    """A callback that raises for one path must not block emits for other paths,
    and `_mtimes` must still advance so failed paths are not re-emitted forever."""
    seen: list[tuple[str, str]] = []

    async def on_change(path: Path, event_type: str) -> None:
        seen.append((path.name, event_type))
        if path.name == "boom.py":
            raise RuntimeError("simulated callback failure")

    watcher = SkillWatcher(str(tmp_path), on_change=on_change)
    watcher._mtimes = watcher._scan()

    (tmp_path / "boom.py").write_text("a = 1", encoding="utf-8")
    (tmp_path / "ok_a.py").write_text("a = 1", encoding="utf-8")
    (tmp_path / "ok_b.py").write_text("b = 2", encoding="utf-8")

    # Even though the boom.py callback raises, the watcher must keep going and
    # emit "created" for the other two files, and _mtimes must advance.
    await watcher._check_changes()

    names_seen = {name for name, _ in seen}
    assert {"boom.py", "ok_a.py", "ok_b.py"}.issubset(names_seen)
    assert {"created"} == {kind for _, kind in seen}
    assert str(tmp_path / "boom.py") in watcher._mtimes
    assert str(tmp_path / "ok_a.py") in watcher._mtimes
    assert str(tmp_path / "ok_b.py") in watcher._mtimes

    # Subsequent unchanged scan must not re-emit anything (mtimes advanced even
    # for the failing path).
    seen.clear()
    await watcher._check_changes()
    assert seen == []


async def _noop(path: Path, event_type: str) -> None:
    del path, event_type
