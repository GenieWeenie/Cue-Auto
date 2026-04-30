"""SQLite-backed audit trail with export utilities."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class AuditQuery:
    """Filter options for querying audit rows."""

    start_utc: str | None = None
    end_utc: str | None = None
    event: str | None = None
    action: str | None = None
    risk: str | None = None
    outcome: str | None = None
    approval: str | None = None
    user_id: str | None = None
    limit: int = 200


class AuditTrail:
    """Structured audit storage with query, cleanup, and export helpers."""

    def __init__(
        self,
        db_path: str,
        *,
        on_record: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._lock = threading.Lock()
        self._on_record = on_record
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
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

    def __enter__(self) -> AuditTrail:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def record_event(
        self,
        *,
        event_type: str,
        action: str,
        correlation_id: str = "-",
        risk_level: str = "",
        approval_state: str = "",
        outcome: str = "",
        chat_id: str = "",
        user_id: str = "",
        run_id: str = "",
        duration_ms: int = 0,
        details: dict[str, Any] | None = None,
        timestamp_utc: str | None = None,
    ) -> int:
        ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
        payload = json.dumps(details or {}, ensure_ascii=True, separators=(",", ":"))
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO audit_trail (
                    timestamp_utc, correlation_id, event_type, action,
                    risk_level, approval_state, outcome, chat_id, user_id, run_id,
                    duration_ms, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    correlation_id.strip() or "-",
                    event_type.strip()[:64],
                    action.strip()[:128],
                    risk_level.strip()[:32],
                    approval_state.strip()[:32],
                    outcome.strip()[:32],
                    chat_id.strip()[:64],
                    user_id.strip()[:64],
                    run_id.strip()[:64],
                    max(0, int(duration_ms)),
                    payload,
                ),
            )
            self._conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to insert audit row")
            row_id = int(cursor.lastrowid)
        event_dict = {
            "id": row_id,
            "timestamp_utc": ts,
            "correlation_id": correlation_id.strip() or "-",
            "event_type": event_type.strip()[:64],
            "action": action.strip()[:128],
            "risk_level": risk_level.strip()[:32],
            "approval_state": approval_state.strip()[:32],
            "outcome": outcome.strip()[:32],
            "chat_id": chat_id.strip()[:64],
            "user_id": user_id.strip()[:64],
            "run_id": run_id.strip()[:64],
            "duration_ms": max(0, int(duration_ms)),
            "details": details or {},
        }
        if self._on_record is not None:
            t = threading.Thread(target=self._on_record, args=(event_dict,), daemon=True)
            t.start()
        return row_id

    def query(self, query: AuditQuery) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        if query.start_utc:
            conditions.append("timestamp_utc >= ?")
            params.append(_normalize_time_bound(query.start_utc, end=False))
        if query.end_utc:
            conditions.append("timestamp_utc <= ?")
            params.append(_normalize_time_bound(query.end_utc, end=True))
        if query.event:
            conditions.append("event_type = ?")
            params.append(query.event.strip())
        if query.action:
            conditions.append("action = ?")
            params.append(query.action.strip())
        if query.risk:
            conditions.append("risk_level = ?")
            params.append(query.risk.strip())
        if query.outcome:
            conditions.append("outcome = ?")
            params.append(query.outcome.strip())
        if query.approval:
            conditions.append("approval_state = ?")
            params.append(query.approval.strip())
        if query.user_id:
            conditions.append("user_id = ?")
            params.append(query.user_id.strip())

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        capped_limit = max(1, min(2000, query.limit))
        sql = f"SELECT * FROM audit_trail {where_clause} ORDER BY timestamp_utc DESC, id DESC LIMIT ?"
        params.append(capped_limit)

        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def cleanup_older_than(self, retention_days: int, *, now_utc: str | None = None) -> int:
        if retention_days <= 0:
            return 0
        now_dt = _parse_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM audit_trail WHERE timestamp_utc < ?",
                (cutoff,),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    @staticmethod
    def export_records(records: list[dict[str, Any]], fmt: str) -> tuple[str, bytes, str]:
        normalized = fmt.strip().lower()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if normalized == "json":
            payload = json.dumps(
                {
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "count": len(records),
                    "records": records,
                },
                ensure_ascii=True,
                indent=2,
            )
            return f"cue-agent-audit-{ts}.json", payload.encode("utf-8"), "application/json"
        if normalized == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                [
                    "id",
                    "timestamp_utc",
                    "correlation_id",
                    "event_type",
                    "action",
                    "risk_level",
                    "approval_state",
                    "outcome",
                    "chat_id",
                    "user_id",
                    "run_id",
                    "duration_ms",
                    "details_json",
                ]
            )
            for row in records:
                writer.writerow(
                    [
                        row.get("id"),
                        row.get("timestamp_utc"),
                        row.get("correlation_id"),
                        row.get("event_type"),
                        row.get("action"),
                        row.get("risk_level"),
                        row.get("approval_state"),
                        row.get("outcome"),
                        row.get("chat_id"),
                        row.get("user_id"),
                        row.get("run_id"),
                        row.get("duration_ms"),
                        json.dumps(row.get("details", {}), ensure_ascii=True, separators=(",", ":")),
                    ]
                )
            return f"cue-agent-audit-{ts}.csv", buffer.getvalue().encode("utf-8"), "text/csv"
        if normalized in {"markdown", "md"}:
            lines = [
                "# CueAgent Audit Export",
                "",
                f"- Generated at (UTC): `{datetime.now(timezone.utc).isoformat()}`",
                f"- Record count: `{len(records)}`",
                "",
                "| timestamp_utc | event_type | action | user_id | risk | approval | outcome | duration_ms |",
                "| --- | --- | --- | --- | --- | --- | --- | ---: |",
            ]
            for row in records:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            _md_cell(str(row.get("timestamp_utc", ""))),
                            _md_cell(str(row.get("event_type", ""))),
                            _md_cell(str(row.get("action", ""))),
                            _md_cell(str(row.get("user_id", ""))),
                            _md_cell(str(row.get("risk_level", ""))),
                            _md_cell(str(row.get("approval_state", ""))),
                            _md_cell(str(row.get("outcome", ""))),
                            _md_cell(str(row.get("duration_ms", 0))),
                        ]
                    )
                    + " |"
                )
            if not records:
                lines.append("| _none_ | _none_ | _none_ | _none_ | _none_ | _none_ | _none_ | 0 |")
            content = "\n".join(lines)
            return f"cue-agent-audit-{ts}.md", content.encode("utf-8"), "text/markdown"
        raise ValueError(f"Unsupported audit export format: {fmt}")

    def _ensure_schema_locked(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '-',
                event_type TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT '',
                approval_state TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_trail(timestamp_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_event
                ON audit_trail(event_type, timestamp_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_risk_outcome
                ON audit_trail(risk_level, outcome, timestamp_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_user_timestamp
                ON audit_trail(user_id, timestamp_utc DESC);
            """
        )
        self._ensure_column_locked("user_id", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column_locked(self, column_name: str, column_sql: str) -> None:
        rows = self._conn.execute("PRAGMA table_info(audit_trail)").fetchall()
        known = {str(row["name"]) for row in rows}
        if column_name in known:
            return
        self._conn.execute(f"ALTER TABLE audit_trail ADD COLUMN {column_name} {column_sql}")
        self._conn.commit()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        details_raw = str(row["details_json"] or "{}")
        details: dict[str, Any]
        try:
            parsed = json.loads(details_raw)
            details = parsed if isinstance(parsed, dict) else {"raw": parsed}
        except json.JSONDecodeError:
            details = {"raw": details_raw}
        return {
            "id": int(row["id"]),
            "timestamp_utc": str(row["timestamp_utc"]),
            "correlation_id": str(row["correlation_id"]),
            "event_type": str(row["event_type"]),
            "action": str(row["action"]),
            "risk_level": str(row["risk_level"]),
            "approval_state": str(row["approval_state"]),
            "outcome": str(row["outcome"]),
            "chat_id": str(row["chat_id"]),
            "user_id": str(row["user_id"]),
            "run_id": str(row["run_id"]),
            "duration_ms": int(row["duration_ms"]),
            "details": details,
        }


def _parse_utc(value: str | None) -> datetime:
    if value is None:
        raise ValueError("Missing datetime value")
    text = value.strip()
    if not text:
        raise ValueError("Empty datetime value")
    if len(text) == 10:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=timezone.utc)
    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_time_bound(value: str, *, end: bool) -> str:
    text = value.strip()
    if len(text) == 10:
        dt = _parse_utc(text)
        if end:
            dt = dt + timedelta(days=1) - timedelta(microseconds=1)
        return dt.isoformat()
    return _parse_utc(text).isoformat()


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()
