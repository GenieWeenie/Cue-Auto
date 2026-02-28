"""Tests for SQLite audit trail storage and export."""

from __future__ import annotations

import json
import time

from cue_agent.audit import AuditQuery, AuditTrail


def test_audit_trail_records_and_filters():
    trail = AuditTrail(":memory:")
    trail.record_event(
        event_type="tool_execution",
        action="run_shell",
        correlation_id="corr-1",
        risk_level="high",
        approval_state="required",
        outcome="success",
        user_id="u-admin",
        details={"cmd": "ls"},
        timestamp_utc="2026-02-01T10:00:00+00:00",
    )
    trail.record_event(
        event_type="llm_call",
        action="chat_completion",
        correlation_id="corr-2",
        risk_level="low",
        approval_state="not_required",
        outcome="error",
        user_id="u-operator",
        details={"error": "timeout"},
        timestamp_utc="2026-02-02T10:00:00+00:00",
    )

    filtered = trail.query(AuditQuery(event="tool_execution", risk="high", outcome="success", limit=10))
    assert len(filtered) == 1
    assert filtered[0]["action"] == "run_shell"
    assert filtered[0]["details"]["cmd"] == "ls"

    date_filtered = trail.query(AuditQuery(start_utc="2026-02-02", end_utc="2026-02-02", limit=10))
    assert len(date_filtered) == 1
    assert date_filtered[0]["action"] == "chat_completion"

    user_filtered = trail.query(AuditQuery(user_id="u-admin", limit=10))
    assert len(user_filtered) == 1
    assert user_filtered[0]["action"] == "run_shell"


def test_audit_trail_cleanup_deletes_old_rows():
    trail = AuditTrail(":memory:")
    trail.record_event(
        event_type="conversation",
        action="user_message",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    trail.record_event(
        event_type="conversation",
        action="assistant_message",
        timestamp_utc="2026-02-10T00:00:00+00:00",
    )

    deleted = trail.cleanup_older_than(30, now_utc="2026-02-20T00:00:00+00:00")
    assert deleted == 1
    rows = trail.query(AuditQuery(limit=10))
    assert len(rows) == 1
    assert rows[0]["action"] == "assistant_message"


def test_audit_export_formats():
    rows = [
        {
            "id": 1,
            "timestamp_utc": "2026-02-20T00:00:00+00:00",
            "correlation_id": "corr-1",
            "event_type": "tool_execution",
            "action": "read_file",
            "risk_level": "low",
            "approval_state": "not_required",
            "outcome": "success",
            "chat_id": "chat-1",
            "user_id": "u1",
            "run_id": "",
            "duration_ms": 12,
            "details": {"path": "README.md"},
        }
    ]

    json_name, json_payload, _ = AuditTrail.export_records(rows, "json")
    parsed = json.loads(json_payload.decode("utf-8"))
    assert json_name.endswith(".json")
    assert parsed["count"] == 1

    csv_name, csv_payload, _ = AuditTrail.export_records(rows, "csv")
    assert csv_name.endswith(".csv")
    assert "read_file" in csv_payload.decode("utf-8")

    md_name, md_payload, _ = AuditTrail.export_records(rows, "markdown")
    assert md_name.endswith(".md")
    assert "CueAgent Audit Export" in md_payload.decode("utf-8")


def test_audit_trail_on_record_callback_called():
    """When on_record is provided, it is called with the event dict after insert."""
    received: list[dict] = []

    def capture(event: dict) -> None:
        received.append(event)

    trail = AuditTrail(":memory:", on_record=capture)
    trail.record_event(
        event_type="approval",
        action="approved",
        user_id="u1",
        details={"role": "admin"},
        timestamp_utc="2026-02-28T12:00:00+00:00",
    )
    # Callback runs in a daemon thread; give it a moment
    time.sleep(0.05)
    assert len(received) == 1
    assert received[0]["event_type"] == "approval"
    assert received[0]["action"] == "approved"
    assert received[0]["user_id"] == "u1"
    assert received[0]["details"] == {"role": "admin"}
    assert "id" in received[0] and received[0]["id"] >= 1
