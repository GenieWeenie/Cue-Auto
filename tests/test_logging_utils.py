"""Tests for logging setup and correlation helpers."""

from __future__ import annotations

import io
import json
import logging

from cue_agent.logging_utils import correlation_context, get_correlation_id, setup_logging


def test_correlation_context_sets_and_resets():
    assert get_correlation_id() is None
    with correlation_context("corr-123"):
        assert get_correlation_id() == "corr-123"
    assert get_correlation_id() is None


def test_setup_logging_json_format(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setenv("EAP_LOG_FORMAT", "json")
    monkeypatch.setenv("EAP_LOG_LEVEL", "INFO")

    setup_logging(stream=stream)

    logger = logging.getLogger("cue_agent.test")
    with correlation_context("corr-test"):
        logger.info("hello", extra={"event": "test_event", "sample": 5})

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "hello"
    assert payload["correlation_id"] == "corr-test"
    assert payload["event"] == "test_event"
    assert payload["sample"] == 5


def test_setup_logging_per_module_levels(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setenv("EAP_LOG_FORMAT", "text")
    monkeypatch.setenv("CUE_LOG_LEVEL_BRAIN", "DEBUG")
    monkeypatch.setenv("CUE_LOG_LEVEL_LOOP", "WARNING")

    setup_logging(stream=stream)

    assert logging.getLogger("cue_agent.brain").level == logging.DEBUG
    assert logging.getLogger("cue_agent.loop").level == logging.WARNING
