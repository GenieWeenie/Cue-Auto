"""Multi-user role storage and permission checks."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

VALID_ROLES = ("admin", "operator", "user", "readonly")
APPROVER_ROLES = {"admin", "operator"}

ROLE_PERMISSION_MATRIX: dict[str, set[str]] = {
    "admin": {"*"},
    "operator": {
        "chat",
        "help",
        "status",
        "skills",
        "settings",
        "usage",
        "tasks.view",
        "tasks.manage",
        "approve.view",
        "audit.export",
        "users.self",
    },
    "user": {
        "chat",
        "help",
        "status",
        "skills",
        "settings",
        "usage",
        "tasks.view",
        "tasks.manage",
        "approve.view",
        "audit.export",
        "users.self",
    },
    "readonly": {
        "help",
        "status",
        "skills",
        "settings",
        "usage",
        "tasks.view",
        "users.self",
    },
}


def normalize_role(raw_role: str) -> str:
    role = raw_role.strip().lower()
    if role not in VALID_ROLES:
        raise ValueError(f"Unsupported role: {raw_role}")
    return role


def has_permission(role: str, permission: str) -> bool:
    try:
        normalized = normalize_role(role)
    except ValueError:
        return False
    allowed = ROLE_PERMISSION_MATRIX.get(normalized, set())
    return "*" in allowed or permission in allowed


def is_approver(role: str) -> bool:
    try:
        normalized = normalize_role(role)
    except ValueError:
        return False
    return normalized in APPROVER_ROLES


class UserAccessStore:
    """Persist users/roles in SQLite with lightweight query helpers."""

    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._ensure_schema_locked()

    def upsert_user(
        self,
        user_id: str,
        *,
        username: str = "",
        display_name: str = "",
        default_role: str = "user",
        created_by: str = "",
    ) -> dict[str, str]:
        key = user_id.strip()
        if not key:
            raise ValueError("user_id is required")
        role = normalize_role(default_role)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, username, display_name, role, created_at_utc, updated_at_utc, created_by "
                "FROM user_access WHERE user_id = ?",
                (key,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO user_access (
                        user_id, username, display_name, role, created_at_utc, updated_at_utc, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        username.strip()[:128],
                        display_name.strip()[:128],
                        role,
                        now,
                        now,
                        created_by.strip()[:64],
                    ),
                )
                self._conn.commit()
            else:
                new_username = username.strip()[:128] or str(row["username"] or "")
                new_display = display_name.strip()[:128] or str(row["display_name"] or "")
                self._conn.execute(
                    """
                    UPDATE user_access
                    SET username = ?, display_name = ?, updated_at_utc = ?
                    WHERE user_id = ?
                    """,
                    (new_username, new_display, now, key),
                )
                self._conn.commit()
            updated = self._conn.execute(
                "SELECT user_id, username, display_name, role, created_at_utc, updated_at_utc, created_by "
                "FROM user_access WHERE user_id = ?",
                (key,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("Failed to load updated user row")
        return _row_to_dict(updated)

    def get_user(self, user_id: str) -> dict[str, str] | None:
        key = user_id.strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, username, display_name, role, created_at_utc, updated_at_utc, created_by "
                "FROM user_access WHERE user_id = ?",
                (key,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_users(self, *, limit: int = 200) -> list[dict[str, str]]:
        capped = max(1, min(2000, limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT user_id, username, display_name, role, created_at_utc, updated_at_utc, created_by
                FROM user_access
                ORDER BY role ASC, updated_at_utc DESC
                LIMIT ?
                """,
                (capped,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def set_role(self, user_id: str, role: str, *, actor_user_id: str = "") -> dict[str, str]:
        normalized = normalize_role(role)
        self.upsert_user(
            user_id,
            default_role=normalized,
            created_by=actor_user_id,
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE user_access
                SET role = ?, updated_at_utc = ?, created_by = CASE
                    WHEN created_by = '' THEN ?
                    ELSE created_by
                END
                WHERE user_id = ?
                """,
                (normalized, now, actor_user_id.strip()[:64], user_id.strip()),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT user_id, username, display_name, role, created_at_utc, updated_at_utc, created_by "
                "FROM user_access WHERE user_id = ?",
                (user_id.strip(),),
            ).fetchone()
        if updated is None:
            raise RuntimeError("Failed to update user role")
        return _row_to_dict(updated)

    def delete_user(self, user_id: str) -> bool:
        key = user_id.strip()
        if not key:
            return False
        with self._lock:
            cursor = self._conn.execute("DELETE FROM user_access WHERE user_id = ?", (key,))
            self._conn.commit()
            return int(cursor.rowcount or 0) > 0

    def has_any_role(self, role: str) -> bool:
        normalized = normalize_role(role)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM user_access WHERE role = ? LIMIT 1",
                (normalized,),
            ).fetchone()
        return row is not None

    def role_counts(self) -> dict[str, int]:
        counts = {role: 0 for role in VALID_ROLES}
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, COUNT(*) AS total FROM user_access GROUP BY role",
            ).fetchall()
        for row in rows:
            role = str(row["role"])
            if role in counts:
                counts[role] = int(row["total"])
        return counts

    def total_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS total FROM user_access").fetchone()
        return int(row["total"]) if row is not None else 0

    def _ensure_schema_locked(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_access (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_user_access_role
                ON user_access(role);
            CREATE INDEX IF NOT EXISTS idx_user_access_updated
                ON user_access(updated_at_utc DESC);
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, str]:
    return {
        "user_id": str(row["user_id"] or ""),
        "username": str(row["username"] or ""),
        "display_name": str(row["display_name"] or ""),
        "role": str(row["role"] or "user"),
        "created_at_utc": str(row["created_at_utc"] or ""),
        "updated_at_utc": str(row["updated_at_utc"] or ""),
        "created_by": str(row["created_by"] or ""),
    }
