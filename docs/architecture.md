# CueAgent Architecture

This document summarizes how orchestration, audit, and notifications fit into CueAgent. For the high-level block diagram and setup, see the [README](../README.md).

## Orchestration (multi-agent)

The **orchestration** layer (`src/cue_agent/orchestration/`) handles delegation from the primary agent to sub-agents for parallel or scoped work:

- **SubAgentSpec** — Defines a sub-agent run: prompt, optional skill scopes, provider preference, timeout.
- **SubAgentResult** — Captures outcome (completed/failed/timeout/killed) and usage/cost for the parent.
- **Multi-agent orchestrator** — Manages a queue of sub-agent requests, runs them (with concurrency limits), and hands results back to the main loop.

Sub-agents inherit the same safety and approval constraints (risk classifier, approval gate). Configuration: `CUE_MULTI_AGENT_ENABLED`, `CUE_MULTI_AGENT_MAX_CONCURRENT`, `CUE_MULTI_AGENT_SUBAGENT_TIMEOUT_SECONDS`, `CUE_MULTI_AGENT_DEFAULT_PROVIDER_PREFERENCE`.

## Audit

The **audit** layer (`src/cue_agent/audit/`) provides a structured, queryable record of what the agent did:

- **AuditTrail** — SQLite-backed store. Records events (tool execution, LLM calls, approvals, errors) with correlation ID, risk level, approval state, outcome, and optional user ID.
- **Query and export** — Filter by date, event type, risk, outcome, user. Export as JSON, CSV, or Markdown (CLI and Telegram `/audit`).
- **Retention** — Configurable retention and daily cleanup cron (`CUE_AUDIT_RETENTION_DAYS`, `CUE_AUDIT_CLEANUP_CRON`).

All high-risk actions and approvals flow through the risk/approval pipeline and are written to the audit trail for compliance and debugging.

## Notifications

The **notifications** layer (`src/cue_agent/notifications/`) delivers operational alerts to the Telegram admin chat:

- **NotificationManager** — Queues events (task completion, failures, approval requests, provider issues, budget warnings). Supports priority (low/medium/high/critical) and filtering via `CUE_NOTIFICATION_PRIORITY_THRESHOLD`.
- **Delivery modes** — `immediate` (send as they happen, with quiet-hours for non-critical), `hourly`, or `daily` digest. Quiet-hours and timezone are configurable.
- **Batching** — In digest modes, events are grouped and sent on a cron schedule to avoid flooding the chat.

Notification content is produced by the loop, heartbeat, approval gateway, and health/budget checks; the manager handles rate limiting and delivery.

## Data flow (summary)

1. **User/loop** → Telegram or Ralph loop produces intent and tool plans.
2. **Risk/approval** — High-risk steps go through the approval gate; outcomes are recorded in the audit trail.
3. **Execution** — Tools run (including optional sub-agent delegation); results are logged and may trigger notifications.
4. **Audit** — Every significant event is written to the audit trail for later query and export.
5. **Notifications** — Selected events are pushed to the admin chat according to delivery mode and priority.

## Workflows

Workflow definitions live in YAML files under the workflows directory and are loaded by **WorkflowLoader** (`src/cue_agent/workflows/loader.py`). The **WorkflowManager** discovers files, resolves triggers (manual, schedules, events), and runs workflows via the **WorkflowEngine**.

### Workflow template variables

Workflow YAML can contain template placeholders that are replaced when a workflow is loaded. This allows the same file to be parameterized by environment or by input (e.g. from Telegram).

- **Placeholder syntax:** Use `{{ VAR }}` or `{{ VAR_NAME }}` in string values (one identifier between double braces). Keys are not substituted.
- **Passing variables:** The loader accepts an optional `variables` dict:
  - `WorkflowLoader.load_file(path, variables={"ENV": "production", "USER": "alice"})` — pass a dict when loading a single file.
  - `WorkflowLoader.load_all(variables=...)` — pass the same dict when loading all workflows.
- **Typical sources for the dict:**
  - Environment: e.g. `os.environ.get("CUE_WORKFLOW_VAR_FOO")` for a variable named `FOO`.
  - Telegram or other input: when starting a workflow from a command or form, merge user-provided values into the variables dict before calling the loader.
- **Behavior:** Any `{{ NAME }}` in a string value is replaced by `variables.get("NAME", "")`. Missing variables are replaced with an empty string. Substitution is applied recursively to the whole loaded structure (including steps and nested fields).

For deployment and operations (backups, secrets, health), see the [deployment guide](deployment.md) and [security](security.md) doc.
