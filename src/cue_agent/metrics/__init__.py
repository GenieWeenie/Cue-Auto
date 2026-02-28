"""Optional Prometheus metrics: request count, latency, LLM usage by provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

_METRIC_PREFIX = "cue_"

_registry = CollectorRegistry()
_http_requests_total = Counter(
    f"{_METRIC_PREFIX}http_requests_total",
    "Total HTTP requests to the health server",
    ["path"],
    registry=_registry,
)
_http_request_duration_seconds = Histogram(
    f"{_METRIC_PREFIX}http_request_duration_seconds",
    "Request duration in seconds",
    ["path"],
    registry=_registry,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Mutable ref for usage getter at scrape time (set by get_prometheus_text, read by collector).
_usage_getter_ref: list[Callable[[], dict[str, Any]] | None] = []


class _UsageSummaryCollector:
    """Collector that yields LLM usage metrics from the router usage summary."""

    def collect(self) -> Any:
        getter = _usage_getter_ref[0] if _usage_getter_ref else None
        if not getter:
            return
        try:
            summary = getter()
        except Exception:
            return
        if not isinstance(summary, dict):
            return
        providers = summary.get("providers")
        if not isinstance(providers, dict):
            return

        requests = CounterMetricFamily(
            f"{_METRIC_PREFIX}llm_requests_total",
            "Total LLM requests by provider",
            labels=["provider"],
        )
        tokens_in = CounterMetricFamily(
            f"{_METRIC_PREFIX}llm_tokens_input_total",
            "Total input tokens by provider",
            labels=["provider"],
        )
        tokens_out = CounterMetricFamily(
            f"{_METRIC_PREFIX}llm_tokens_output_total",
            "Total output tokens by provider",
            labels=["provider"],
        )
        cost = GaugeMetricFamily(
            f"{_METRIC_PREFIX}llm_estimated_cost_usd",
            "Estimated cost in USD by provider (cumulative)",
            labels=["provider"],
        )
        for name, data in providers.items():
            if not isinstance(data, dict):
                continue
            provider = str(name)
            req = data.get("requests")
            req_val = int(req) if isinstance(req, (int, float)) else 0
            requests.add_metric([provider], req_val)
            ti = data.get("tokens_in")
            tokens_in.add_metric([provider], int(ti) if isinstance(ti, (int, float)) else 0)
            to = data.get("tokens_out")
            tokens_out.add_metric([provider], int(to) if isinstance(to, (int, float)) else 0)
            c = data.get("estimated_cost_usd")
            cost.add_metric([provider], float(c) if isinstance(c, (int, float)) else 0.0)
        yield requests
        yield tokens_in
        yield tokens_out
        yield cost


_usage_collector = _UsageSummaryCollector()
_registry.register(_usage_collector)


def record_request(path: str, duration_seconds: float) -> None:
    """Record one HTTP request for the given path and duration."""
    path_label = path if path else "/"
    _http_requests_total.labels(path=path_label).inc()
    _http_request_duration_seconds.labels(path=path_label).observe(duration_seconds)


def get_prometheus_text(usage_summary_getter: Callable[[], dict[str, Any]] | None = None) -> bytes:
    """Return Prometheus exposition format (text) for the current metrics.

    If usage_summary_getter is provided, it is used to populate LLM usage metrics
    (requests, tokens, cost by provider) for this scrape.
    """
    if usage_summary_getter is not None:
        _usage_getter_ref.append(usage_summary_getter)
    try:
        out = generate_latest(_registry)
        return out if out is not None else b""
    finally:
        if usage_summary_getter is not None and _usage_getter_ref:
            _usage_getter_ref.pop()
