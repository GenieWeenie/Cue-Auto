"""Persistent SQLite-backed task queue with priorities and dependencies."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_STATUS_PENDING = "pending"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELED = "canceled"

_MUTABLE_STATUSES = (
    TASK_STATUS_PENDING,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_FAILED,
)


class TaskQueue:
    """SQLite queue for persistent prioritized work items."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema_locked()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass  # already closed

    def __enter__(self) -> TaskQueue:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def create_task(
        self,
        title: str,
        *,
        description: str = "",
        priority: int = 3,
        parent_task_id: int | None = None,
        source: str = "user",
        depends_on: list[int] | None = None,
    ) -> int:
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Task title must not be empty")
        if priority < 1 or priority > 4:
            raise ValueError("Priority must be between 1 (highest) and 4 (lowest)")

        with self._lock:
            if parent_task_id is not None and not self._task_exists_locked(parent_task_id):
                raise ValueError(f"Parent task does not exist: {parent_task_id}")

            now = _utcnow()
            cursor = self._conn.execute(
                """
                INSERT INTO tasks (
                    title, description, priority, status, parent_task_id, source,
                    attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    cleaned_title,
                    description.strip(),
                    priority,
                    TASK_STATUS_PENDING,
                    parent_task_id,
                    source,
                    now,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to create task record")
            task_id = int(cursor.lastrowid)

            for dep_id in depends_on or []:
                if dep_id == task_id:
                    raise ValueError("Task cannot depend on itself")
                if not self._task_exists_locked(dep_id):
                    raise ValueError(f"Dependency task does not exist: {dep_id}")
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id)
                    VALUES (?, ?)
                    """,
                    (task_id, dep_id),
                )

            self._refresh_blocked_states_locked(now=now)
            self._conn.commit()
            return task_id

    def create_subtask(
        self,
        parent_task_id: int,
        title: str,
        *,
        description: str = "",
        priority: int = 3,
        source: str = "agent_subtask",
    ) -> int:
        return self.create_task(
            title,
            description=description,
            priority=priority,
            parent_task_id=parent_task_id,
            source=source,
        )

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        if task_id == depends_on_task_id:
            raise ValueError("Task cannot depend on itself")

        with self._lock:
            if not self._task_exists_locked(task_id):
                raise ValueError(f"Task does not exist: {task_id}")
            if not self._task_exists_locked(depends_on_task_id):
                raise ValueError(f"Dependency task does not exist: {depends_on_task_id}")

            self._conn.execute(
                """
                INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id)
                VALUES (?, ?)
                """,
                (task_id, depends_on_task_id),
            )
            self._refresh_blocked_states_locked(now=_utcnow())
            self._conn.commit()

    def list_tasks(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        capped_limit = max(1, limit)
        with self._lock:
            self._refresh_blocked_states_locked(now=_utcnow())

            if status is None:
                rows = self._conn.execute(
                    """
                    SELECT * FROM tasks
                    ORDER BY
                        CASE status
                            WHEN 'in_progress' THEN 0
                            WHEN 'pending' THEN 1
                            WHEN 'blocked' THEN 2
                            WHEN 'failed' THEN 3
                            WHEN 'done' THEN 4
                            ELSE 5
                        END,
                        priority ASC,
                        created_at ASC
                    LIMIT ?
                    """,
                    (capped_limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = ?
                    ORDER BY priority ASC, created_at ASC
                    LIMIT ?
                    """,
                    (status, capped_limit),
                ).fetchall()

            task_ids = [int(row["id"]) for row in rows]
            dep_map = self._dependency_map_locked(task_ids)
            return [self._row_to_task(row, dep_map.get(int(row["id"]), [])) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            dep_map = self._dependency_map_locked([task_id])
            return self._row_to_task(row, dep_map.get(task_id, []))

    def next_unblocked_task(self) -> dict[str, Any] | None:
        with self._lock:
            self._refresh_blocked_states_locked(now=_utcnow())
            row = self._conn.execute(
                """
                SELECT t.*
                FROM tasks t
                WHERE t.status = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM task_dependencies td
                      JOIN tasks dep ON dep.id = td.depends_on_task_id
                      WHERE td.task_id = t.id
                        AND dep.status != ?
                  )
                ORDER BY t.priority ASC, t.created_at ASC
                LIMIT 1
                """,
                (TASK_STATUS_PENDING, TASK_STATUS_DONE),
            ).fetchone()
            if row is None:
                return None

            task_id = int(row["id"])
            dep_map = self._dependency_map_locked([task_id])
            return self._row_to_task(row, dep_map.get(task_id, []))

    def mark_in_progress(self, task_id: int) -> None:
        with self._lock:
            now = _utcnow()
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?,
                    attempt_count = attempt_count + 1, last_error = ''
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    TASK_STATUS_IN_PROGRESS,
                    now,
                    now,
                    task_id,
                    TASK_STATUS_PENDING,
                    TASK_STATUS_BLOCKED,
                    TASK_STATUS_FAILED,
                ),
            )
            self._conn.commit()

    def mark_done(self, task_id: int) -> None:
        with self._lock:
            now = _utcnow()
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, completed_at = ?, updated_at = ?, last_error = ''
                WHERE id = ? AND status IN (?, ?, ?, ?)
                """,
                (
                    TASK_STATUS_DONE,
                    now,
                    now,
                    task_id,
                    *_MUTABLE_STATUSES,
                ),
            )
            self._refresh_blocked_states_locked(now=now)
            self._conn.commit()

    def mark_failed(self, task_id: int, error: str, retry_limit: int) -> str:
        with self._lock:
            row = self._conn.execute("SELECT attempt_count FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Task does not exist: {task_id}")
            attempts = int(row["attempt_count"])
            should_retry = attempts < max(0, retry_limit)
            next_status = TASK_STATUS_PENDING if should_retry else TASK_STATUS_FAILED
            now = _utcnow()
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (next_status, now, error.strip()[:1000], task_id),
            )
            self._refresh_blocked_states_locked(now=now)
            self._conn.commit()
            return next_status

    def retry_task(self, task_id: int) -> None:
        with self._lock:
            now = _utcnow()
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, attempt_count = 0, last_error = ''
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    TASK_STATUS_PENDING,
                    now,
                    task_id,
                    TASK_STATUS_FAILED,
                    TASK_STATUS_CANCELED,
                ),
            )
            self._refresh_blocked_states_locked(now=now)
            self._conn.commit()

    def cancel_task(self, task_id: int, reason: str = "") -> bool:
        """Cancel a task. Returns True if the task was found in a cancellable state."""
        with self._lock:
            now = _utcnow()
            cursor = self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, last_error = ?
                WHERE id = ? AND status IN (?, ?, ?, ?)
                """,
                (
                    TASK_STATUS_CANCELED,
                    now,
                    ("canceled: " + reason.strip())[:1000] if reason else "canceled",
                    task_id,
                    TASK_STATUS_PENDING,
                    TASK_STATUS_BLOCKED,
                    TASK_STATUS_IN_PROGRESS,
                    TASK_STATUS_FAILED,
                ),
            )
            if cursor.rowcount == 0:
                return False
            self._refresh_blocked_states_locked(now=now)
            self._conn.commit()
            return True

    def child_count(self, parent_task_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE parent_task_id = ?",
                (parent_task_id,),
            ).fetchone()
            return int(row["count"]) if row is not None else 0

    def list_child_tasks(self, parent_task_id: int, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        capped_limit = max(1, limit)
        with self._lock:
            self._refresh_blocked_states_locked(now=_utcnow())
            params: tuple[Any, ...]
            query = """
                SELECT * FROM tasks
                WHERE parent_task_id = ?
            """
            params = (parent_task_id,)
            if status is not None:
                query += " AND status = ?"
                params = (parent_task_id, status)
            query += " ORDER BY priority ASC, created_at ASC LIMIT ?"
            params = (*params, capped_limit)
            rows = self._conn.execute(query, params).fetchall()
            task_ids = [int(row["id"]) for row in rows]
            dep_map = self._dependency_map_locked(task_ids)
            return [self._row_to_task(row, dep_map.get(int(row["id"]), [])) for row in rows]

    def queue_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status",
            ).fetchall()
            stats = {
                "pending": 0,
                "blocked": 0,
                "in_progress": 0,
                "failed": 0,
                "done": 0,
                "canceled": 0,
            }
            for row in rows:
                status = str(row["status"])
                if status in stats:
                    stats[status] = int(row["count"])
            stats["total"] = sum(stats.values())
            return stats

    def _task_exists_locked(self, task_id: int) -> bool:
        row = self._conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row is not None

    def _ensure_schema_locked(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 4),
                status TEXT NOT NULL DEFAULT 'pending',
                parent_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                source TEXT NOT NULL DEFAULT 'user',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                PRIMARY KEY (task_id, depends_on_task_id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_priority_created
                ON tasks(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent
                ON tasks(parent_task_id);
            """
        )

    def _refresh_blocked_states_locked(self, now: str) -> None:
        unmet_condition = """
            EXISTS (
                SELECT 1
                FROM task_dependencies td
                JOIN tasks dep ON dep.id = td.depends_on_task_id
                WHERE td.task_id = tasks.id
                  AND dep.status != 'done'
            )
        """
        self._conn.execute(
            f"""
            UPDATE tasks
            SET status = ?, updated_at = ?
            WHERE status = ?
              AND {unmet_condition}
            """,
            (TASK_STATUS_BLOCKED, now, TASK_STATUS_PENDING),
        )
        self._conn.execute(
            f"""
            UPDATE tasks
            SET status = ?, updated_at = ?
            WHERE status = ?
              AND NOT {unmet_condition}
            """,
            (TASK_STATUS_PENDING, now, TASK_STATUS_BLOCKED),
        )

    def _dependency_map_locked(self, task_ids: list[int]) -> dict[int, list[int]]:
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        rows = self._conn.execute(
            f"""
            SELECT task_id, depends_on_task_id
            FROM task_dependencies
            WHERE task_id IN ({placeholders})
            """,
            tuple(task_ids),
        ).fetchall()
        mapping: dict[int, list[int]] = {task_id: [] for task_id in task_ids}
        for row in rows:
            task_id = int(row["task_id"])
            mapping.setdefault(task_id, []).append(int(row["depends_on_task_id"]))
        return mapping

    def _row_to_task(self, row: sqlite3.Row, depends_on: list[int]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "title": str(row["title"]),
            "description": str(row["description"]),
            "priority": int(row["priority"]),
            "status": str(row["status"]),
            "parent_task_id": int(row["parent_task_id"]) if row["parent_task_id"] is not None else None,
            "source": str(row["source"]),
            "attempt_count": int(row["attempt_count"]),
            "last_error": str(row["last_error"]),
            "depends_on": depends_on,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "started_at": str(row["started_at"]) if row["started_at"] else None,
            "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
        }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
