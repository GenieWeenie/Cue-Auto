# CueAgent

[![CI](https://github.com/your-username/Cue-Auto/actions/workflows/ci.yml/badge.svg)](https://github.com/your-username/Cue-Auto/actions/workflows/ci.yml)

Autonomous AI agent built on the [Efficient Agent Protocol (EAP)](https://github.com/GenieWeenie/efficient-agent-protocol). CueAgent combines a cascading multi-provider LLM brain, Telegram interface, hot-reloadable skills, optional long-term vector memory, and a Ralph-style autonomous loop into a single cohesive system.

## Architecture

CueAgent is organized into 6 blocks, all wired together by the `CueApp` orchestrator:

```
+------------------+      +-----------------+      +------------------+
|     Brain        |      |     Comms       |      |     Memory       |
| LLMRouter        |      | TelegramGateway |      | SessionMemory    |
| CueBrain         |<---->| ApprovalGateway |      | (EAP StateManager|
| SoulLoader       |      | Normalizer      |      |  conversations)  |
+------------------+      +-----------------+      +------------------+
         |                        |                         |
         v                        v                         v
+------------------+      +-----------------+      +------------------+
|    Actions       |      |    Security     |      |    Heartbeat     |
| ActionRegistry   |      | RiskClassifier  |      | APScheduler      |
| 5 built-in tools |      | ApprovalGate    |      | Daily summary    |
| + loaded skills  |      | (EAP HITL)      |      | Health check     |
+------------------+      +-----------------+      +------------------+
                                  |
                    +----------------------------+
                    |      Ralph Loop            |
                    | orient -> pick -> plan ->  |
                    | approve -> execute ->      |
                    | verify -> commit           |
                    +----------------------------+
```

### Brain

- **LLMRouter** — Cascading fallback across 4 providers: OpenAI (primary) -> Anthropic Claude -> OpenRouter -> LM Studio (local). Implements EAP's `LLMProvider` interface so EAP never knows about the fallback chain.
- **CueBrain** — Wraps EAP's `AgentClient` with SOUL identity injection. Provides `chat()` for interactive conversation and `plan()` for autonomous task planning.
- **SoulLoader** — Reads `SOUL.md` (agent personality/rules) and injects it into every LLM prompt. Caches with mtime checks for live editing.

### Comms

- **TelegramGateway** — Telegram bot interface using `python-telegram-bot`. Supports polling and webhook modes.
- **ApprovalGateway** — Sends inline-keyboard approval prompts to the admin chat for high-risk actions with Approve/Reject/Details controls.
- **MessageNormalizer** — Converts platform-specific messages into `UnifiedMessage` format.

### Memory

- **SessionMemory** — Wraps EAP's `StateManager` conversation API. Maintains per-chat sliding-window context (last 20 turns by default).
- **VectorMemory** — Optional ChromaDB-backed semantic recall for long-term memory across turns and loop outcomes.
- **Consolidation policy** — Periodic summarization/compaction keeps recent raw entries while rolling older entries into durable summary memories.

### Actions

- **ActionRegistry** — Wraps EAP's `ToolRegistry`. Registers 5 built-in tools plus any loaded skills.
- **Built-in tools**: `send_telegram`, `web_search`, `read_file`, `write_file`, `run_shell`
- **Web search tool**: provider-backed search with fallback chain `tavily -> serpapi -> duckduckgo`, plus deduped relevance ranking.

### Security

- **RiskClassifier** — Context-aware risk engine with `low|medium|high|critical` levels using tool args, intent, and execution context.
- **Risk rules file** — JSON policy file (`skills/risk_rules.json` by default) with custom shell/path risk patterns and approval levels.
- **ApprovalGate** — Bridges EAP's HITL (human-in-the-loop) checkpoints to Telegram approval buttons.

### Heartbeat

- **Scheduler** — APScheduler async cron for recurring tasks.
- **Tasks** — Daily summary (tasks/tools/costs/health/errors), periodic health checks, and notification digests.

### Ralph Loop

An autonomous outer loop inspired by [ralph-wiggum](https://github.com/ghuntley/ralph-wiggum):

1. **Orient** — Build context from recent memory and system state
2. **Pick** — LLM-driven task selection (or detect "nothing to do")
3. **Plan** — Generate an EAP macro (DAG of tool calls) for the chosen task
4. **Approve** — Inject HITL checkpoints for high-risk steps
5. **Execute** — Run the macro via EAP's `AsyncLocalExecutor`
6. **Verify** — Post-execution verification
7. **Commit** — Log the outcome to memory

## Project Structure

```
Cue-Auto/
├── Dockerfile
├── docker-compose.yml
├── src/cue_agent/
│   ├── app.py                 # Orchestrator — wires all blocks together
│   ├── config.py              # CueConfig (pydantic-settings, .env loading)
│   ├── __main__.py            # CLI entry point
│   ├── brain/
│   │   ├── llm_router.py      # Cascading LLM provider fallback
│   │   ├── cue_brain.py       # AgentClient wrapper with SOUL injection
│   │   └── soul_loader.py     # Reads and caches SOUL.md
│   ├── comms/
│   │   ├── telegram_gateway.py
│   │   ├── approval_gateway.py
│   │   ├── normalizer.py
│   │   └── models.py          # UnifiedMessage, UnifiedResponse
│   ├── memory/
│   │   ├── session_memory.py  # EAP StateManager wrapper
│   │   └── vector_memory.py   # Optional ChromaDB semantic memory
│   ├── actions/
│   │   ├── registry.py        # EAP ToolRegistry wrapper + skills
│   │   ├── builtin_tools.py   # 5 built-in tool implementations
│   │   └── schemas.py         # JSON schemas for built-in tools
│   ├── security/
│   │   ├── risk_classifier.py
│   │   └── approval_gate.py
│   ├── loop/
│   │   ├── ralph_loop.py      # Autonomous outer loop
│   │   ├── task_picker.py     # LLM-driven task selection
│   │   └── verifier.py        # Post-execution verification
│   ├── heartbeat/
│   │   ├── scheduler.py       # APScheduler wrapper
│   │   └── tasks.py           # Scheduled task implementations
│   ├── health/
│   │   └── server.py          # Health endpoint + optional web dashboard
│   └── skills/
│       ├── loader.py          # Discovers and loads skills
│       └── watcher.py         # Filesystem polling for hot-reload
├── skills/                    # Drop skills here (auto-discovered)
│   └── example_hello.py
├── tests/
├── SOUL.md                    # Agent identity and personality
├── .env.example               # All configuration variables
├── .env.production.example    # Production-ready Docker/systemd template
├── docs/deployment.md         # Docker, systemd, and cloud deployment guide
└── pyproject.toml
```

## Setup

### Requirements

- Python 3.11 - 3.13 (EAP requires `<3.14`)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) for the messaging interface
- At least one LLM provider API key (OpenAI, Anthropic, or OpenRouter), or LM Studio running locally

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/Cue-Auto.git
cd Cue-Auto

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate
# Activate (macOS/Linux)
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Optional: install vector memory backend
pip install -e ".[dev,vector]"
```

### Configuration

```bash
# Copy the example env file
cp .env.example .env
```

Edit `.env` with your keys. At minimum you need one LLM provider:

```env
# Primary LLM
CUE_OPENAI_API_KEY=sk-your-key-here

# Telegram (required for interactive mode)
CUE_TELEGRAM_BOT_TOKEN=your-bot-token
CUE_TELEGRAM_ADMIN_CHAT_ID=123456789
```

All configuration uses the `CUE_` prefix. See `.env.example` for the full list of options.
Set `CUE_VECTOR_MEMORY_ENABLED=true` after installing the `vector` extra to enable semantic recall.
Consolidation is controlled by `CUE_VECTOR_MEMORY_CONSOLIDATION_*` settings and runs on cron when heartbeat is enabled.
Web search behavior is controlled by `CUE_SEARCH_*` settings and provider keys (`CUE_TAVILY_API_KEY`, `CUE_SERPAPI_API_KEY`).

### Verify Setup

```bash
python -m cue_agent --check-config
```

This prints the status of all providers, loaded skills, and feature flags.

### One-command Docker Deploy

```bash
cp .env.production.example .env.production
# edit .env.production with your secrets
docker compose up -d --build
```

Health endpoint:

```bash
curl http://localhost:8080/healthz
```

Dashboard (when enabled):

```bash
# browser
open http://localhost:8080/dashboard
```

For full production instructions (Docker, systemd, Railway/Fly.io/DigitalOcean), see [`docs/deployment.md`](docs/deployment.md).

Webhook + automatic SSL (Caddy) compose variant:

```bash
WEBHOOK_DOMAIN=bot.example.com \
docker compose -f docker-compose.yml -f docker-compose.webhook.yml up -d --build
```

## Running

### Interactive Mode (Telegram Polling)

```bash
python -m cue_agent --mode polling
```

Chat with CueAgent directly through Telegram. The bot configures a command menu (`/help`, `/status`, `/tasks`, `/skills`, `/usage`, `/approve`, `/settings`, `/audit`, `/users`, `/market`) and renders rich inline views with navigation buttons. High-risk actions trigger inline Approve/Reject/Details controls.

### Webhook Mode (Telegram HTTPS)

```bash
cue-agent --mode webhook
```

Required environment for webhook mode:

```env
CUE_TELEGRAM_WEBHOOK_URL=https://your-domain.example/telegram/webhook
CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN=replace-with-random-secret
CUE_TELEGRAM_WEBHOOK_LISTEN_HOST=0.0.0.0
CUE_TELEGRAM_WEBHOOK_LISTEN_PORT=8081
CUE_TELEGRAM_WEBHOOK_PATH=/telegram/webhook
```

Incoming webhook requests are rejected unless `X-Telegram-Bot-Api-Secret-Token` matches `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN`.

### Autonomous Loop

```bash
# Run continuously
python -m cue_agent --mode loop

# Run a single iteration
python -m cue_agent --mode once
```

The Ralph loop runs autonomously — picking tasks, planning, executing, and verifying — with human approval gates for anything risky.

### Both Together

Set `CUE_LOOP_ENABLED=true` in `.env` and run in polling mode. The Telegram interface and autonomous loop run concurrently — you can chat with the agent while it works autonomously in the background.

### Web Monitoring Dashboard

Enable dashboard mode via environment variables:

```env
CUE_DASHBOARD_ENABLED=true
CUE_DASHBOARD_USERNAME=admin
CUE_DASHBOARD_PASSWORD=replace-this
```

The dashboard is served on the health endpoint port and protected by HTTP Basic Auth. Routes:
- `/dashboard` — runtime summary (status, uptime, current task, provider health)
- `/dashboard/actions` — action timeline with tool/risk/duration/outcome
- `/dashboard/tasks` — queue stats and task list
- `/dashboard/providers` — provider status and usage metrics

### Telegram Commands

Use these in Telegram chat:

- `/help` — command center with quick navigation actions
- `/status` — runtime health snapshot
- `/skills` — loaded skill summary
- `/settings` — runtime settings snapshot
- `/approve` — pending approval queue
- `/usage` — monthly provider usage, estimated spend, and budget thresholds
- `/audit json|csv|markdown [event=...] [risk=...] [outcome=...] [user=...] [start=YYYY-MM-DD] [end=YYYY-MM-DD]` — export filtered audit trail
- `/users me|list|role|remove` — inspect and manage user-role access
- `/market search|install|update` — community registry workflow
- `/tasks` — list queued tasks
- `/tasks pending|blocked|in_progress|failed|done|all`
- `/tasks download|export|json` — export tasks as a JSON attachment
- `/task add [p1|p2|p3|p4] <title>`
- `/task sub <parent_id> [p1|p2|p3|p4] <title>`
- `/task done <task_id>`
- `/task depend <task_id> <depends_on_task_id>`
- `/task retry <task_id>`

Upload files directly in Telegram to attach context; attachment metadata is normalized and routed through the unified message layer.

### CLI Audit Export

Export audit trail data directly from CLI:

```bash
# Print JSON to stdout
cue-agent --export-audit-format json --audit-limit 200

# Write Markdown export to file with filters
cue-agent --export-audit-format markdown \
  --audit-output ./data/audit.md \
  --audit-event tool_execution \
  --audit-risk high \
  --audit-user 123456789 \
  --audit-outcome error \
  --audit-start 2026-02-01 \
  --audit-end 2026-02-28
```

### Notification Delivery

Operational notifications are sent to the Telegram admin chat for:
- task completion/failure
- high-risk actions requiring approval
- provider outages
- budget warnings and hard-stop events

Delivery behavior is configurable:
- `immediate` — send events as they happen (quiet-hours respected for non-critical alerts)
- `hourly` — batch into hourly digest messages
- `daily` — batch into daily digest messages

## Multi-User Access

CueAgent supports multi-user RBAC roles persisted in SQLite:
- `admin` — full access including user-role management
- `operator` — operations access including approvals and audit export
- `user` — normal usage (chat, tasks, status, usage, skills)
- `readonly` — view-only status/tasks/skills/usage

User management commands:
- `/users me`
- `/users list`
- `/users role <user_id> <admin|operator|user|readonly>`
- `/users remove <user_id>`

Approval callbacks are only accepted from `admin` or `operator` users.
Audit trail records include `user_id` for per-user tracking and filtering (`/audit ... user=<id>`).

## Skills

Skills extend CueAgent with new capabilities. Drop files into the `skills/` directory and they're auto-discovered at startup. With hot-reload enabled (default), new or modified skills are picked up within 2 seconds without restarting.

### Skill SDK Quickstart

```bash
# Generate a skill pack scaffold
cue-agent create-skill daily_brief

# Generate a single-file scaffold
cue-agent create-skill ops_note --style simple
```

For full SDK docs (manifest/schema patterns, prompt/config patterns, testing harness, typing, troubleshooting), see [`docs/skills-sdk.md`](docs/skills-sdk.md).

### Simple Skill (single `.py` file)

```python
# skills/weather.py

SKILL_MANIFEST = {
    "name": "weather",
    "description": "Get current weather for a location",
    "tools": [
        {
            "name": "get_weather",
            "schema": {
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"}
                    },
                    "required": ["location"],
                    "additionalProperties": False,
                }
            }
        }
    ]
}

def get_weather(location: str) -> dict:
    """Fetch weather for a city."""
    return {"location": location, "temp": "72F", "condition": "sunny"}
```

Convention:
- Define a `SKILL_MANIFEST` dict at module level
- Implement functions whose names match each entry in `tools[].name`
- Functions receive keyword arguments matching the schema properties and return a `dict`

### Skill Pack (folder with multiple files)

For more complex skills that need a system prompt or configuration:

```
skills/research_topic/
├── skill.py        # Required: SKILL_MANIFEST + tool functions
├── prompt.md       # Optional: system prompt for this skill
└── config.yaml     # Optional: key-value config (api_key, timeout, etc.)
```

The loader reads `prompt.md` as a string and `config.yaml` as a `key: value` dict, attaching both to the loaded skill metadata.

### Isolated Skill Testing

Use the testing harness to validate a skill without running CueAgent:

```python
from cue_agent.skills.testing import SkillTestHarness, MockSkillContext

harness = SkillTestHarness.from_path("skills/research_topic/skill.py")
result = harness.run_tool(
    "run",
    task="prepare release brief",
    context=MockSkillContext(user_id="u1", chat_id="ops-room"),
)
print(result)
```

### Example Skills

Realistic examples are included in `skills/examples/`:
- `research_brief.py`
- `release_readiness.py`
- `incident_timeline.py`

### Hot Reload

When `CUE_SKILLS_HOT_RELOAD=true` (default), a background watcher polls the skills directory every 2 seconds:
- **New** `.py` files or folders with `skill.py` are loaded automatically
- **Modified** files trigger a reload (re-imports the module)
- **Deleted** files trigger an unload (tools removed from registry)

## Skill Marketplace

CueAgent includes a local community registry flow for search/install/update with metadata and validation:

CLI:
- `cue-agent marketplace search <query>`
- `cue-agent marketplace install <skill_id> [--version X.Y.Z]`
- `cue-agent marketplace update <skill_id|all>`
- `cue-agent marketplace validate-registry`
- `cue-agent marketplace validate-submission <path>`

Telegram:
- `/market search <query>`
- `/market install <skill_id> [version]`
- `/market update [skill_id|all]`

Registry and package defaults:
- `skills/registry/index.json`
- `skills/registry_packages/`

Submission validation checks:
- manifest shape and tool/function mapping
- semver + CueAgent compatibility constraints
- basic security scan for disallowed dangerous patterns
- docs presence requirements

## LLM Provider Cascade

CueAgent classifies requests as simple vs complex and chooses provider order accordingly:

| Priority | Provider | Config Key | Notes |
|----------|----------|------------|-------|
| 1 | OpenAI | `CUE_OPENAI_API_KEY` | Primary, GPT-4o default |
| 2 | Anthropic | `CUE_ANTHROPIC_API_KEY` | Claude models |
| 3 | OpenRouter | `CUE_OPENROUTER_API_KEY` | Aggregator, any model |
| 4 | LM Studio | `CUE_LMSTUDIO_BASE_URL` | Local, always available |

Routing behavior:
- Simple prompts prioritize cheaper/faster providers (LM Studio/OpenRouter first).
- Complex prompts prioritize stronger models (OpenAI/Anthropic first).
- Router records per-provider request/token/latency/cost metrics.
- Monthly budget warning and hard-stop thresholds are enforced via `CUE_LLM_BUDGET_*`.
- Use `/usage` in Telegram to see spend by provider.

Only providers with configured API keys (or reachable URLs for LM Studio) are added to the cascade. If all providers fail, or all remote providers are budget-blocked, an outage error is raised.

## Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=cue_agent --cov-report=term-missing
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `CUE_OPENAI_API_KEY` | `""` | OpenAI API key |
| `CUE_OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `CUE_OPENAI_BASE_URL` | `https://api.openai.com` | OpenAI base URL |
| `CUE_ANTHROPIC_API_KEY` | `""` | Anthropic API key |
| `CUE_ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Claude model name |
| `CUE_OPENROUTER_API_KEY` | `""` | OpenRouter API key |
| `CUE_OPENROUTER_MODEL` | `openai/gpt-4o` | OpenRouter model name |
| `CUE_OPENROUTER_BASE_URL` | `https://openrouter.ai/api` | OpenRouter base URL |
| `CUE_LMSTUDIO_BASE_URL` | `http://localhost:1234` | LM Studio server URL |
| `CUE_LMSTUDIO_MODEL` | `local-model` | LM Studio model name |
| `CUE_LLM_TEMPERATURE` | `0.0` | LLM temperature |
| `CUE_LLM_TIMEOUT_SECONDS` | `60` | LLM request timeout |
| `CUE_LLM_BUDGET_WARNING_USD` | `20.0` | Monthly estimated spend warning threshold |
| `CUE_LLM_MONTHLY_BUDGET_USD` | `50.0` | Monthly estimated spend hard-stop threshold |
| `CUE_LLM_BUDGET_ENFORCE_HARD_STOP` | `true` | Skip remote providers when budget hard-stop is exceeded |
| `CUE_LLM_COST_OPENAI_INPUT_PER_1K` | `0.005` | Estimated OpenAI input token cost (USD per 1k) |
| `CUE_LLM_COST_OPENAI_OUTPUT_PER_1K` | `0.015` | Estimated OpenAI output token cost (USD per 1k) |
| `CUE_LLM_COST_ANTHROPIC_INPUT_PER_1K` | `0.003` | Estimated Anthropic input token cost (USD per 1k) |
| `CUE_LLM_COST_ANTHROPIC_OUTPUT_PER_1K` | `0.015` | Estimated Anthropic output token cost (USD per 1k) |
| `CUE_LLM_COST_OPENROUTER_INPUT_PER_1K` | `0.003` | Estimated OpenRouter input token cost (USD per 1k) |
| `CUE_LLM_COST_OPENROUTER_OUTPUT_PER_1K` | `0.010` | Estimated OpenRouter output token cost (USD per 1k) |
| `CUE_LLM_COST_LMSTUDIO_INPUT_PER_1K` | `0.0` | Estimated LM Studio input token cost (USD per 1k) |
| `CUE_LLM_COST_LMSTUDIO_OUTPUT_PER_1K` | `0.0` | Estimated LM Studio output token cost (USD per 1k) |
| `CUE_TELEGRAM_BOT_TOKEN` | `""` | Telegram bot token |
| `CUE_TELEGRAM_ADMIN_CHAT_ID` | `0` | Admin chat ID for approvals |
| `CUE_TELEGRAM_ADMIN_USER_IDS` | `[]` | Explicit admin user IDs for RBAC bootstrap |
| `CUE_TELEGRAM_OPERATOR_USER_IDS` | `[]` | Operator user IDs allowed to approve high-risk actions |
| `CUE_NOTIFICATIONS_ENABLED` | `true` | Enable operational Telegram notifications |
| `CUE_NOTIFICATION_DELIVERY_MODE` | `immediate` | Notification delivery mode: `immediate`, `hourly`, or `daily` |
| `CUE_NOTIFICATION_PRIORITY_THRESHOLD` | `medium` | Minimum notification priority to send |
| `CUE_NOTIFICATION_QUIET_HOURS_START` | `22` | Quiet-hours start hour (0-23) in notification timezone |
| `CUE_NOTIFICATION_QUIET_HOURS_END` | `7` | Quiet-hours end hour (0-23) in notification timezone |
| `CUE_NOTIFICATION_TIMEZONE` | `UTC` | IANA timezone for quiet-hours evaluation |
| `CUE_NOTIFICATION_HOURLY_DIGEST_CRON` | `0 * * * *` | Cron schedule for hourly/catch-up notification digest |
| `CUE_NOTIFICATION_DAILY_DIGEST_CRON` | `0 8 * * *` | Cron schedule for daily notification digest |
| `CUE_STATE_DB_PATH` | `cue_state.db` | SQLite state database path |
| `CUE_SOUL_MD_PATH` | `SOUL.md` | Agent identity file path |
| `CUE_SKILLS_DIR` | `skills` | Skills directory path |
| `CUE_SKILLS_HOT_RELOAD` | `true` | Enable skill hot-reloading |
| `CUE_SKILLS_REGISTRY_INDEX_PATH` | `skills/registry/index.json` | Marketplace registry index path |
| `CUE_SKILLS_REGISTRY_PACKAGES_DIR` | `skills/registry_packages` | Marketplace packaged skill source directory |
| `CUE_SKILLS_REGISTRY_STATE_PATH` | `skills/.marketplace-installed.json` | Installed marketplace skill state file |
| `CUE_HIGH_RISK_TOOLS` | `["run_shell","write_file","send_telegram"]` | Tools requiring approval |
| `CUE_APPROVAL_REQUIRED_LEVELS` | `["high","critical"]` | Risk levels that trigger mandatory approval |
| `CUE_RISK_RULES_PATH` | `skills/risk_rules.json` | Path to JSON risk policy rules file |
| `CUE_RISK_SANDBOX_DRY_RUN` | `false` | Auto-deny non-low-risk approvals for safe policy testing |
| `CUE_REQUIRE_APPROVAL` | `true` | Enable HITL approval gates |
| `CUE_MULTI_USER_ENABLED` | `true` | Enable multi-user role-based access control |
| `CUE_MULTI_USER_BOOTSTRAP_FIRST_USER` | `true` | Auto-promote first seen user to admin only when no admin exists |
| `CUE_HEARTBEAT_ENABLED` | `false` | Enable scheduled tasks |
| `CUE_DAILY_SUMMARY_CRON` | `0 8 * * *` | Cron for daily summary |
| `CUE_LOOP_ENABLED` | `false` | Enable autonomous loop alongside Telegram |
| `CUE_LOOP_INTERVAL_SECONDS` | `30` | Seconds between loop iterations |
| `CUE_TASK_QUEUE_ENABLED` | `true` | Enable persistent SQLite task queue scheduling |
| `CUE_TASK_QUEUE_MAX_LIST` | `20` | Max tasks returned by `/tasks` |
| `CUE_TASK_QUEUE_RETRY_FAILED_ATTEMPTS` | `2` | Retry attempts before marking task failed |
| `CUE_TASK_QUEUE_AUTO_SUBTASKS_ENABLED` | `true` | Allow loop agent to generate sub-tasks |
| `CUE_TASK_QUEUE_AUTO_SUBTASKS_MAX` | `3` | Max auto-generated sub-tasks per parent task |
| `CUE_HEALTHCHECK_ENABLED` | `true` | Enable `/healthz` endpoint for probes |
| `CUE_HEALTHCHECK_HOST` | `0.0.0.0` | Health endpoint bind host |
| `CUE_HEALTHCHECK_PORT` | `8080` | Health endpoint bind port |
| `CUE_DASHBOARD_ENABLED` | `false` | Enable authenticated web monitoring dashboard |
| `CUE_DASHBOARD_USERNAME` | `admin` | Basic auth username for dashboard routes |
| `CUE_DASHBOARD_PASSWORD` | `change-me` | Basic auth password for dashboard routes |
| `CUE_DASHBOARD_TIMELINE_LIMIT` | `200` | Max in-memory action timeline entries |
| `CUE_TELEGRAM_WEBHOOK_URL` | `""` | Public HTTPS webhook URL registered with Telegram |
| `CUE_TELEGRAM_WEBHOOK_LISTEN_HOST` | `0.0.0.0` | Local bind host for webhook listener |
| `CUE_TELEGRAM_WEBHOOK_LISTEN_PORT` | `8081` | Local bind port for webhook listener |
| `CUE_TELEGRAM_WEBHOOK_PATH` | `/telegram/webhook` | Local HTTP path accepted for Telegram webhook POSTs |
| `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN` | `""` | Required secret token verified against Telegram header |
| `CUE_TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES` | `false` | Drop pending Telegram updates when setting webhook |
| `CUE_AUDIT_RETENTION_DAYS` | `30` | Days to keep audit records before cleanup |
| `CUE_AUDIT_CLEANUP_CRON` | `15 3 * * *` | Daily cron for audit retention cleanup |

## License

MIT
