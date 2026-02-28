"""Tests for persistent task queue behavior."""

from __future__ import annotations

from cue_agent.loop.task_queue import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TaskQueue,
)


def test_task_queue_persists_across_restart(tmp_path):
    db_path = str(tmp_path / "queue.db")
    q1 = TaskQueue(db_path)
    task_id = q1.create_task("Persist me", priority=2)
    assert task_id == 1

    q2 = TaskQueue(db_path)
    rows = q2.list_tasks()
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["title"] == "Persist me"


def test_task_queue_priority_and_dependencies(tmp_path):
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    low_id = queue.create_task("Low priority first task", priority=3)
    high_blocked_id = queue.create_task("High but blocked", priority=1, depends_on=[low_id])

    next_task = queue.next_unblocked_task()
    assert next_task is not None
    assert next_task["id"] == low_id
    assert next_task["status"] == TASK_STATUS_PENDING

    queue.mark_done(low_id)
    next_after_done = queue.next_unblocked_task()
    assert next_after_done is not None
    assert next_after_done["id"] == high_blocked_id


def test_task_queue_failed_retry_policy(tmp_path):
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    task_id = queue.create_task("Retry policy task", priority=2)

    queue.mark_in_progress(task_id)
    status_after_first = queue.mark_failed(task_id, "boom 1", retry_limit=2)
    assert status_after_first == TASK_STATUS_PENDING

    queue.mark_in_progress(task_id)
    status_after_second = queue.mark_failed(task_id, "boom 2", retry_limit=2)
    assert status_after_second == TASK_STATUS_FAILED

    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TASK_STATUS_FAILED
    assert "boom 2" in task["last_error"]


def test_task_queue_subtasks_and_listing(tmp_path):
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    parent_id = queue.create_task("Parent task", priority=2)
    child_id = queue.create_subtask(parent_id, "Child task", priority=3)

    assert queue.child_count(parent_id) == 1
    rows = queue.list_tasks(limit=10)
    by_id = {row["id"]: row for row in rows}
    assert by_id[parent_id]["parent_task_id"] is None
    assert by_id[child_id]["parent_task_id"] == parent_id
