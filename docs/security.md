# CueAgent Security and Operational Security

This document covers risk controls, approval flows, and key operational practices.

## Risk classification

CueAgent uses a **context-aware risk engine** (`RiskClassifier`) that assigns each tool execution a level: `low`, `medium`, `high`, or `critical`. Inputs include:

- Tool name (e.g. `run_shell`, `write_file`, `send_telegram` are inherently higher risk)
- Tool arguments (paths, commands, recipients)
- Optional **risk rules file** — JSON policy at `CUE_RISK_RULES_PATH` (default `skills/risk_rules.json`) with custom patterns (e.g. shell commands, path allow/deny) and required approval levels

Configuration:

- `CUE_HIGH_RISK_TOOLS` — List of tool names that are always treated as high risk unless overridden by rules.
- `CUE_APPROVAL_REQUIRED_LEVELS` — Risk levels that trigger mandatory human approval (default `high`, `critical`).
- `CUE_RISK_SANDBOX_DRY_RUN` — If `true`, non-low-risk approvals are auto-denied (useful for testing policy without approving).

## Approval flow (HITL)

- When the agent proposes a step that meets `CUE_APPROVAL_REQUIRED_LEVELS`, the **ApprovalGate** injects a human-in-the-loop checkpoint.
- The **ApprovalGateway** (Telegram) sends an inline keyboard: Approve / Reject / Details. Only users with `admin` or `operator` role can approve.
- Until approval (or rejection), the macro does not proceed. Rejected steps are logged; approved steps are executed and recorded in the audit trail.

Set `CUE_REQUIRE_APPROVAL=false` to disable approval gates (not recommended for production with risky tools).

## Multi-user and RBAC

- **Multi-user** is enabled by default (`CUE_MULTI_USER_ENABLED`). Users are stored in SQLite with roles: `admin`, `operator`, `user`, `readonly`.
- **Admin/operator** — Can approve high-risk actions and manage user roles. Bootstrap: `CUE_MULTI_USER_BOOTSTRAP_FIRST_USER=true` promotes the first seen user to admin when no admin exists.
- **User** — Normal chat, tasks, status, usage, skills. **Readonly** — View-only access.
- Audit trail records `user_id` for filtering and accountability (`/audit ... user=<id>`).

## Operational practices

- **Secrets** — Never commit `.env` or `.env.production`. Rotate Telegram bot token and LLM API keys periodically; see [deployment](deployment.md#secrets-rotation) for rotation notes.
- **Dashboard** — When `CUE_DASHBOARD_ENABLED=true`, protect `/dashboard` with strong `CUE_DASHBOARD_USERNAME` / `CUE_DASHBOARD_PASSWORD` and restrict access (e.g. VPN or firewall) when exposed.
- **Webhook** — Use a long, random `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN` and do not reuse the bot token as the webhook secret.
- **Backups** — Back up `./data/cue_state.db` (and optionally `./data/vector_memory` if using vector memory); see [deployment](deployment.md#backup-and-runbook).

For deployment options (Docker, systemd, cloud) and health/dashboard usage, see the [deployment guide](deployment.md).
