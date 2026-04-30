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

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
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

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        setup_logging(stream=stream)

    assert logging.getLogger("cue_agent.brain").level == logging.DEBUG
    assert logging.getLogger("cue_agent.loop").level == logging.WARNING


def test_setup_logging_cue_log_level_canonical(monkeypatch):
    """CUE_LOG_LEVEL and CUE_LOG_FORMAT are the canonical env var names."""
    stream = io.StringIO()
    monkeypatch.setenv("CUE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CUE_LOG_FORMAT", "json")

    setup_logging(stream=stream)

    root = logging.getLogger()
    assert root.level == logging.DEBUG

    logger = logging.getLogger("cue_agent.canonical_test")
    with correlation_context("corr-canon"):
        logger.info("canonical test")

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "canonical test"
    assert payload["correlation_id"] == "corr-canon"


def test_setup_logging_eap_log_level_deprecated_fallback(monkeypatch):
    """EAP_LOG_LEVEL still works as a fallback and emits DeprecationWarning."""
    stream = io.StringIO()
    monkeypatch.delenv("CUE_LOG_LEVEL", raising=False)
    monkeypatch.setenv("EAP_LOG_LEVEL", "WARNING")
    monkeypatch.delenv("CUE_LOG_FORMAT", raising=False)
    monkeypatch.delenv("EAP_LOG_FORMAT", raising=False)

    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        setup_logging(stream=stream)

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_setup_logging_cue_log_level_takes_priority(monkeypatch):
    """CUE_LOG_LEVEL takes priority over EAP_LOG_LEVEL."""
    stream = io.StringIO()
    monkeypatch.setenv("CUE_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("EAP_LOG_LEVEL", "DEBUG")

    setup_logging(stream=stream)

    root = logging.getLogger()
    assert root.level == logging.ERROR
