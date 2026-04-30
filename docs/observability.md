# Observability and production logging

This document describes what CueAgent logs, how to use it for cost/latency/errors, and optional extensions for production.

## What is logged today

- **Application lifecycle** — Startup, shutdown, and mode (polling/webhook/loop). Log level is controlled by `CUE_LOG_LEVEL` (or deprecated `EAP_LOG_LEVEL`) and optional `CUE_LOG_LEVEL_*` per-module overrides (e.g. `CUE_LOG_LEVEL_APP`, `CUE_LOG_LEVEL_BRAIN`).
- **LLM usage** — The LLM router records per-provider request counts, token usage, latency, and estimated cost. These are exposed on the **health endpoint** (`/healthz`) and in the **dashboard** (when enabled) under provider metrics. Use `/usage` in Telegram for monthly spend and budget thresholds.
- **Tool execution** — Tool calls and outcomes are recorded in the **audit trail** (event type, action, risk, outcome). Export via `/audit` or `cue-agent --export-audit-format`.
- **Approvals** — Approval requests and decisions are written to the audit trail and can trigger notifications.
- **Errors** — Exceptions and retry/circuit-breaker behavior are logged; critical failures can trigger notifications to the admin chat.
- **Heartbeat** — When enabled, daily summary and health-check tasks log summary stats (tasks, tools, costs, errors).

## Cost and latency

- **Cost** — Estimated per-request cost is computed from token counts and `CUE_LLM_COST_*_PER_1K` settings. Monthly totals and budget warnings/hard-stops are enforced; see `/usage` and `CUE_LLM_BUDGET_*`.
- **Latency** — Request latency is tracked per provider in the router and surfaced in dashboard/provider views. For deeper analysis, enable DEBUG logging for the brain module (e.g. `CUE_LOG_LEVEL_BRAIN=DEBUG`) in non-production or sample in production.
- **Structured logs** — When `CUE_LOG_FORMAT=json` (or deprecated `EAP_LOG_FORMAT=json`) is set, log output is JSON; you can ship it to a log aggregator (e.g. Datadog, Loki, CloudWatch) and query by correlation ID or module.

## Errors and alerts

- **Notifications** — Operational notifications (task failure, approval required, provider outage, budget warning) are sent to the Telegram admin chat. Configure delivery mode (`immediate` / `hourly` / `daily`) and priority threshold via `CUE_NOTIFICATION_*`.
- **Health endpoint** — `/healthz` includes provider status and (in webhook mode) webhook diagnostics. Use it for alerting when a provider is down or the webhook is misconfigured.
- **Audit trail** — Filter by `outcome=error` or event type to inspect failures; export for post-mortems.

## Prometheus /metrics (optional)

When metrics are enabled, the health server exposes a **`/metrics`** endpoint in Prometheus text exposition format.

### Enabling and disabling

- **Env vars** (see `.env.example`):
  - `CUE_METRICS_ENABLED` — set to `true` to expose `/metrics` (default: `false`).
  - `CUE_METRICS_TYPE` — `prometheus` (expose `/metrics`), `statsd` (reserved for future push), or `none`.
- With `CUE_METRICS_ENABLED=false` or `CUE_METRICS_TYPE` not `prometheus`, `GET /metrics` returns **404** and a JSON body `{"error": "metrics_disabled"}`.

### Where /metrics is served

- `/metrics` is served from the **same HTTP server as the health endpoint** (host/port from `CUE_HEALTHCHECK_HOST` and `CUE_HEALTHCHECK_PORT`), i.e. the same process that serves `/healthz`, `/health`, and the optional dashboard.

### Metrics exposed

- **HTTP (health server):**
  - `cue_http_requests_total{path}` — total request count per path (e.g. `/healthz`, `/dashboard`, `/metrics` is not counted).
  - `cue_http_request_duration_seconds{path}` — request latency histogram per path.
- **LLM (from router usage summary):**
  - `cue_llm_requests_total{provider}` — total LLM requests per provider (openai, anthropic, openrouter, lmstudio).
  - `cue_llm_tokens_input_total{provider}` — total input tokens per provider.
  - `cue_llm_tokens_output_total{provider}` — total output tokens per provider.
  - `cue_llm_estimated_cost_usd{provider}` — estimated cumulative cost in USD per provider.

### Scraping

- Point a Prometheus server (or compatible scraper) at `http://<host>:<healthcheck_port>/metrics`. No authentication is applied to `/metrics`; protect the endpoint at the network or reverse-proxy layer if needed.

## Optional extensions

- **StatsD** — `CUE_METRICS_TYPE=statsd` is reserved for a future push-based integration; only `prometheus` is implemented.
- **Tracing** — Correlation IDs are used in the audit trail; for distributed tracing you could propagate a trace ID through the app and log it in the audit and structured logs.

For deployment, health checks, and dashboard setup, see the [deployment guide](deployment.md).
