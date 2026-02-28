"""Tests for audit export (webhook / S3)."""

from __future__ import annotations

from unittest.mock import patch

from cue_agent.audit.export import export_audit_event


def test_export_audit_event_none_no_op():
    """export_type 'none' or empty does not call HTTP or S3."""
    event = {"id": 1, "event_type": "tool", "action": "read_file"}
    with patch("cue_agent.audit.export.httpx") as mock_httpx:
        export_audit_event(event, export_type="none")
        export_audit_event(event, export_type="")
        mock_httpx.Client.assert_not_called()


def test_export_audit_event_webhook_posts_json():
    """export_type webhook POSTs event as JSON to the given URL."""
    event = {"id": 42, "event_type": "approval", "action": "approved", "user_id": "u1"}
    url = "https://example.com/audit"
    with patch("cue_agent.audit.export.httpx") as mock_httpx:
        mock_resp = mock_httpx.Client.return_value.__enter__.return_value.post.return_value
        mock_resp.status_code = 200
        export_audit_event(
            event,
            export_type="webhook",
            webhook_url=url,
        )
        mock_httpx.Client.return_value.__enter__.return_value.post.assert_called_once()
        call_kw = mock_httpx.Client.return_value.__enter__.return_value.post.call_args[1]
        assert call_kw["json"] == event
        assert call_kw["headers"]["Content-Type"] == "application/json"
        assert mock_httpx.Client.return_value.__enter__.return_value.post.call_args[0][0] == url


def test_export_audit_event_webhook_empty_url_no_request():
    """Webhook with empty URL does not perform a request."""
    event = {"id": 1, "event_type": "x", "action": "y"}
    with patch("cue_agent.audit.export.httpx") as mock_httpx:
        export_audit_event(event, export_type="webhook", webhook_url="")
        mock_httpx.Client.assert_not_called()


def test_export_audit_event_s3_empty_bucket_no_upload():
    """S3 export with empty bucket returns without calling boto3."""
    event = {"id": 1, "event_type": "tool", "action": "run", "timestamp_utc": "2026-02-28T12:00:00+00:00"}
    with patch("cue_agent.audit.export.boto3", create=True) as mock_boto3:
        export_audit_event(
            event,
            export_type="s3",
            s3_bucket="",
            s3_prefix="audit",
        )
        mock_boto3.client.assert_not_called()
