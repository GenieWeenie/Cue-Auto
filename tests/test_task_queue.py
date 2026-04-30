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


def test_task_queue_list_child_tasks_by_status(tmp_path):
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    parent_id = queue.create_task("Parent", priority=2)
    pending_child_id = queue.create_subtask(parent_id, "Pending child", priority=3)
    done_child_id = queue.create_subtask(parent_id, "Done child", priority=3)
    queue.mark_done(done_child_id)

    pending_rows = queue.list_child_tasks(parent_id, status=TASK_STATUS_PENDING, limit=10)
    all_rows = queue.list_child_tasks(parent_id, status=None, limit=10)

    assert [row["id"] for row in pending_rows] == [pending_child_id]
    assert {row["id"] for row in all_rows} == {pending_child_id, done_child_id}


def test_task_queue_context_manager(tmp_path):
    """TaskQueue supports context manager protocol."""
    db_path = str(tmp_path / "queue.db")
    with TaskQueue(db_path) as queue:
        task_id = queue.create_task("Context manager task", priority=2)
        assert queue.get_task(task_id) is not None
    # After context exit, connection is closed
    # Operations should fail
    import sqlite3

    try:
        queue.get_task(task_id)
        assert False, "Expected ProgrammingError after close"
    except sqlite3.ProgrammingError:
        pass  # expected


def test_task_queue_close_idempotent(tmp_path):
    """Calling close() twice does not raise."""
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    queue.create_task("Task", priority=2)
    queue.close()
    queue.close()  # should not raise


def test_task_queue_cancel_task(tmp_path):
    """cancel_task sets status to canceled and returns True."""
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    task_id = queue.create_task("Cancel me", priority=2)

    assert queue.cancel_task(task_id, reason="not needed") is True
    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == "canceled"
    assert "not needed" in task["last_error"]


def test_task_queue_cancel_done_task_fails(tmp_path):
    """cancel_task returns False for already-done tasks."""
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    task_id = queue.create_task("Already done", priority=2)
    queue.mark_done(task_id)

    assert queue.cancel_task(task_id) is False
    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == "done"


def test_task_queue_cancel_in_progress_task(tmp_path):
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    task_id = queue.create_task("In progress", priority=2)
    queue.mark_in_progress(task_id)

    assert queue.cancel_task(task_id, reason="aborted") is True
    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == "canceled"


def test_task_queue_retry_resets_attempt_count(tmp_path):
    """retry_task resets attempt_count and last_error."""
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    task_id = queue.create_task("Retry me", priority=2)

    # Fail it with retry_limit=0 so it stays failed
    queue.mark_in_progress(task_id)
    queue.mark_failed(task_id, "permanent error", retry_limit=0)
    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == "failed"
    assert task["attempt_count"] == 1
    assert "permanent error" in task["last_error"]

    # Retry it
    queue.retry_task(task_id)
    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == "pending"
    assert task["attempt_count"] == 0
    assert task["last_error"] == ""


def test_task_queue_recover_stale_in_progress_reverts_old_tasks(tmp_path):
    """Stale `in_progress` tasks (older than the threshold) revert to `pending`
    on startup recovery; fresh in-progress tasks are left untouched."""
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)

    stale_id = queue.create_task("Stale", priority=2)
    fresh_id = queue.create_task("Fresh", priority=2)
    pending_id = queue.create_task("Pending — should not be affected", priority=2)

    queue.mark_in_progress(stale_id)
    queue.mark_in_progress(fresh_id)

    # Backdate stale task: rewrite both started_at and updated_at to two hours ago.
    queue._conn.execute(
        "UPDATE tasks SET started_at = ?, updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", stale_id),
    )
    queue._conn.commit()

    recovered = queue.recover_stale_in_progress(stale_after_seconds=60)
    assert len(recovered) == 1
    assert recovered[0]["id"] == stale_id

    stale = queue.get_task(stale_id)
    assert stale is not None
    assert stale["status"] == "pending"
    assert stale["started_at"] is None
    assert "stale in_progress on startup" in stale["last_error"]

    # Fresh in-progress task is preserved.
    fresh = queue.get_task(fresh_id)
    assert fresh is not None
    assert fresh["status"] == "in_progress"

    # Untouched pending task stays pending.
    pending = queue.get_task(pending_id)
    assert pending is not None
    assert pending["status"] == "pending"

    # Idempotent: a second call recovers nothing.
    assert queue.recover_stale_in_progress(stale_after_seconds=60) == []
    queue.close()


def test_task_queue_recover_stale_disabled_when_threshold_zero(tmp_path):
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    task_id = queue.create_task("Stale candidate", priority=2)
    queue.mark_in_progress(task_id)
    queue._conn.execute(
        "UPDATE tasks SET started_at = ?, updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", task_id),
    )
    queue._conn.commit()

    assert queue.recover_stale_in_progress(stale_after_seconds=0) == []
    task = queue.get_task(task_id)
    assert task is not None
    assert task["status"] == "in_progress"
    queue.close()


def test_task_queue_wal_mode_enabled(tmp_path):
    """File-backed databases use WAL journal mode."""
    db_path = str(tmp_path / "queue.db")
    queue = TaskQueue(db_path)
    row = queue._conn.execute("PRAGMA journal_mode").fetchone()
    assert str(row[0]) == "wal"
    queue.close()
