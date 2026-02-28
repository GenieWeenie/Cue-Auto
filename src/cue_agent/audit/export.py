"""Optional audit export to webhook or S3 for compliance."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def export_audit_event(
    event: dict[str, Any], *, export_type: str, webhook_url: str = "", s3_bucket: str = "", s3_prefix: str = "audit"
) -> None:
    """Send a single audit event to the configured export target. Runs synchronously; call from a thread to avoid blocking.

    - export_type "webhook": POST event as JSON to webhook_url.
    - export_type "s3": upload event as JSON to s3_bucket with key s3_prefix/YYYY-MM-DD/{id}.json (requires boto3).
    """
    kind = (export_type or "").strip().lower()
    if kind == "webhook":
        _export_webhook(event, webhook_url)
    elif kind == "s3":
        _export_s3(event, s3_bucket=s3_bucket, s3_prefix=s3_prefix)
    # "none" or unknown: no-op


def _export_webhook(event: dict[str, Any], url: str) -> None:
    if not url or not url.strip():
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url.strip(),
                json=event,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Audit export webhook returned %s for event id=%s",
                    resp.status_code,
                    event.get("id"),
                    extra={"event": "audit_export_webhook_error", "status": resp.status_code},
                )
    except Exception:
        logger.exception(
            "Audit export webhook failed for event id=%s",
            event.get("id"),
            extra={"event": "audit_export_webhook_exception"},
        )


def _export_s3(event: dict[str, Any], *, s3_bucket: str, s3_prefix: str) -> None:
    if not s3_bucket or not s3_bucket.strip():
        return
    try:
        import boto3
    except ImportError:
        logger.warning(
            "Audit export to S3 skipped: boto3 not installed. Install with: pip install boto3",
            extra={"event": "audit_export_s3_no_boto3"},
        )
        return
    bucket = s3_bucket.strip()
    prefix = (s3_prefix or "audit").strip().rstrip("/")
    event_id = event.get("id")
    ts = event.get("timestamp_utc", "")
    if ts and len(ts) >= 10:
        date_part = ts[:10]
    else:
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{prefix}/{date_part}/{event_id}.json"
    body = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    try:
        client = boto3.client("s3")
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    except Exception:
        logger.exception(
            "Audit export S3 failed for event id=%s key=%s",
            event_id,
            key,
            extra={"event": "audit_export_s3_exception"},
        )
