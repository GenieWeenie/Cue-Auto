"""Logging configuration and correlation-id helpers."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, TextIO
from uuid import uuid4

_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cue_correlation_id",
    default=None,
)

_STANDARD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "correlation_id",
}


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


@contextmanager
def correlation_context(correlation_id: str) -> Iterator[None]:
    token = _CORRELATION_ID.set(correlation_id)
    try:
        yield
    finally:
        _CORRELATION_ID.reset(token)


def new_correlation_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def _normalize_level(level_name: str | None, default: int = logging.INFO) -> int:
    if not level_name:
        return default
    value = getattr(logging, level_name.upper(), None)
    return value if isinstance(value, int) else default


def _apply_module_log_levels() -> None:
    prefix = "CUE_LOG_LEVEL_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue

        module_key = key[len(prefix) :].strip()
        if not module_key:
            continue

        level = _normalize_level(value)
        if module_key.upper() == "ROOT":
            logging.getLogger().setLevel(level)
            continue

        module_name = module_key.lower().replace("__", ".")
        logging.getLogger(f"cue_agent.{module_name}").setLevel(level)


def setup_logging(stream: TextIO | None = None) -> None:
    """Configure root logging with optional JSON output."""
    # CUE_LOG_LEVEL / CUE_LOG_FORMAT are canonical; EAP_* retained as deprecated fallback.
    # TODO: Remove EAP_* fallback after one release cycle.
    log_level_raw = os.getenv("CUE_LOG_LEVEL") or os.getenv("EAP_LOG_LEVEL")
    log_format_raw = os.getenv("CUE_LOG_FORMAT") or os.getenv("EAP_LOG_FORMAT")
    if os.getenv("EAP_LOG_LEVEL") and not os.getenv("CUE_LOG_LEVEL"):
        import warnings

        warnings.warn(
            "EAP_LOG_LEVEL is deprecated; use CUE_LOG_LEVEL instead",
            DeprecationWarning,
            stacklevel=2,
        )
    if os.getenv("EAP_LOG_FORMAT") and not os.getenv("CUE_LOG_FORMAT"):
        import warnings

        warnings.warn(
            "EAP_LOG_FORMAT is deprecated; use CUE_LOG_FORMAT instead",
            DeprecationWarning,
            stacklevel=2,
        )
    log_level = _normalize_level(log_level_raw, default=logging.INFO)
    log_format = (log_format_raw or "text").lower()

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(CorrelationFilter())

    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s [corr=%(correlation_id)s]: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root.addHandler(handler)
    _apply_module_log_levels()
