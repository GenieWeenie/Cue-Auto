# Observability and production logging

This document describes what CueAgent logs, how to use it for cost/latency/errors, and optional extensions for production.

## What is logged today

- **Application lifecycle** — Startup, shutdown, and mode (polling/webhook/loop). Log level is controlled by `EAP_LOG_LEVEL` and optional `CUE_LOG_LEVEL_*` per-module overrides (e.g. `CUE_LOG_LEVEL_APP`, `CUE_LOG_LEVEL_BRAIN`).
- **LLM usage** — The LLM router records per-provider request counts, token usage, latency, and estimated cost. These are exposed on the **health endpoint** (`/healthz`) and in the **dashboard** (when enabled) under provider metrics. Use `/usage` in Telegram for monthly spend and budget thresholds.
- **Tool execution** — Tool calls and outcomes are recorded in the **audit trail** (event type, action, risk, outcome). Export via `/audit` or `cue-agent --export-audit-format`.
- **Approvals** — Approval requests and decisions are written to the audit trail and can trigger notifications.
- **Errors** — Exceptions and retry/circuit-breaker behavior are logged; critical failures can trigger notifications to the admin chat.
- **Heartbeat** — When enabled, daily summary and health-check tasks log summary stats (tasks, tools, costs, errors).

## Cost and latency

- **Cost** — Estimated per-request cost is computed from token counts and `CUE_LLM_COST_*_PER_1K` settings. Monthly totals and budget warnings/hard-stops are enforced; see `/usage` and `CUE_LLM_BUDGET_*`.
- **Latency** — Request latency is tracked per provider in the router and surfaced in dashboard/provider views. For deeper analysis, enable DEBUG logging for the brain module (e.g. `CUE_LOG_LEVEL_BRAIN=DEBUG`) in non-production or sample in production.
- **Structured logs** — When `EAP_LOG_FORMAT=json` is set, log output is JSON; you can ship it to a log aggregator (e.g. Datadog, Loki, CloudWatch) and query by correlation ID or module.

## Errors and alerts

- **Notifications** — Operational notifications (task failure, approval required, provider outage, budget warning) are sent to the Telegram admin chat. Configure delivery mode (`immediate` / `hourly` / `daily`) and priority threshold via `CUE_NOTIFICATION_*`.
- **Health endpoint** — `/healthz` includes provider status and (in webhook mode) webhook diagnostics. Use it for alerting when a provider is down or the webhook is misconfigured.
- **Audit trail** — Filter by `outcome=error` or event type to inspect failures; export for post-mortems.

## Optional extensions

- **Metrics export** — CueAgent does not currently expose Prometheus/StatsD metrics. To add them, you could instrument the LLM router (request count, latency histograms, token counters) and the audit trail (event counts by type/risk) and expose a `/metrics` endpoint. This would be a future enhancement.
- **Tracing** — Correlation IDs are used in the audit trail; for distributed tracing you could propagate a trace ID through the app and log it in the audit and structured logs.

For deployment, health checks, and dashboard setup, see the [deployment guide](deployment.md).
