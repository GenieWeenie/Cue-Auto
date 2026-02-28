# CueAgent

[![CI](https://github.com/your-username/Cue-Auto/actions/workflows/ci.yml/badge.svg)](https://github.com/your-username/Cue-Auto/actions/workflows/ci.yml)

Autonomous AI agent built on the [Efficient Agent Protocol (EAP)](https://github.com/GenieWeenie/efficient-agent-protocol). CueAgent combines a cascading multi-provider LLM brain, Telegram interface, hot-reloadable skills, and a Ralph-style autonomous loop into a single cohesive system.

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
- **ApprovalGateway** — Sends inline-keyboard approve/deny prompts to the admin chat for high-risk actions.
- **MessageNormalizer** — Converts platform-specific messages into `UnifiedMessage` format.

### Memory

- **SessionMemory** — Wraps EAP's `StateManager` conversation API. Maintains per-chat sliding-window context (last 20 turns by default).

### Actions

- **ActionRegistry** — Wraps EAP's `ToolRegistry`. Registers 5 built-in tools plus any loaded skills.
- **Built-in tools**: `send_telegram`, `web_search`, `read_file`, `write_file`, `run_shell`

### Security

- **RiskClassifier** — Classifies tool calls as high or low risk based on a configurable list.
- **ApprovalGate** — Bridges EAP's HITL (human-in-the-loop) checkpoints to Telegram approval buttons.

### Heartbeat

- **Scheduler** — APScheduler async cron for recurring tasks.
- **Tasks** — Daily summary (sent to admin via Telegram) and periodic health checks.

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
│   │   └── session_memory.py  # EAP StateManager wrapper
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
│   │   └── server.py          # Lightweight HTTP health endpoint (/healthz)
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

For full production instructions (Docker, systemd, Railway/Fly.io/DigitalOcean), see [`docs/deployment.md`](docs/deployment.md).

## Running

### Interactive Mode (Telegram Polling)

```bash
python -m cue_agent --mode polling
```

Chat with CueAgent directly through Telegram. High-risk actions trigger inline approve/deny buttons.

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

## Skills

Skills extend CueAgent with new capabilities. Drop files into the `skills/` directory and they're auto-discovered at startup. With hot-reload enabled (default), new or modified skills are picked up within 2 seconds without restarting.

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

### Hot Reload

When `CUE_SKILLS_HOT_RELOAD=true` (default), a background watcher polls the skills directory every 2 seconds:
- **New** `.py` files or folders with `skill.py` are loaded automatically
- **Modified** files trigger a reload (re-imports the module)
- **Deleted** files trigger an unload (tools removed from registry)

## LLM Provider Cascade

CueAgent tries providers in order, falling back on failure:

| Priority | Provider | Config Key | Notes |
|----------|----------|------------|-------|
| 1 | OpenAI | `CUE_OPENAI_API_KEY` | Primary, GPT-4o default |
| 2 | Anthropic | `CUE_ANTHROPIC_API_KEY` | Claude models |
| 3 | OpenRouter | `CUE_OPENROUTER_API_KEY` | Aggregator, any model |
| 4 | LM Studio | `CUE_LMSTUDIO_BASE_URL` | Local, always available |

Only providers with configured API keys (or reachable URLs for LM Studio) are added to the cascade. If all providers fail, the last error is raised.

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
| `CUE_TELEGRAM_BOT_TOKEN` | `""` | Telegram bot token |
| `CUE_TELEGRAM_ADMIN_CHAT_ID` | `0` | Admin chat ID for approvals |
| `CUE_STATE_DB_PATH` | `cue_state.db` | SQLite state database path |
| `CUE_SOUL_MD_PATH` | `SOUL.md` | Agent identity file path |
| `CUE_SKILLS_DIR` | `skills` | Skills directory path |
| `CUE_SKILLS_HOT_RELOAD` | `true` | Enable skill hot-reloading |
| `CUE_HIGH_RISK_TOOLS` | `["run_shell","write_file","send_telegram"]` | Tools requiring approval |
| `CUE_REQUIRE_APPROVAL` | `true` | Enable HITL approval gates |
| `CUE_HEARTBEAT_ENABLED` | `false` | Enable scheduled tasks |
| `CUE_DAILY_SUMMARY_CRON` | `0 8 * * *` | Cron for daily summary |
| `CUE_LOOP_ENABLED` | `false` | Enable autonomous loop alongside Telegram |
| `CUE_LOOP_INTERVAL_SECONDS` | `30` | Seconds between loop iterations |
| `CUE_HEALTHCHECK_ENABLED` | `true` | Enable `/healthz` endpoint for probes |
| `CUE_HEALTHCHECK_HOST` | `0.0.0.0` | Health endpoint bind host |
| `CUE_HEALTHCHECK_PORT` | `8080` | Health endpoint bind port |

## License

MIT
