# CueAgent Deployment Guide

This guide covers production deployment options for CueAgent. For risk controls, approval flows, and RBAC, see [security](security.md). For logging, cost, and metrics in production, see [observability](observability.md).

1. Docker Compose (recommended)
2. Docker Compose webhook + automatic SSL
3. systemd on a Linux VM
4. Cloud deploy examples (Railway, Fly.io, DigitalOcean App Platform)

## 1) Docker Compose (Recommended)

### Prerequisites

- Docker Engine + Docker Compose plugin installed
- Telegram bot token and admin chat ID
- At least one LLM provider key (OpenAI, Anthropic, OpenRouter, or LM Studio endpoint)

### Steps

```bash
cp .env.production.example .env.production
```

Edit `.env.production` with real secrets.

```bash
docker compose up -d --build
```

### Verify

```bash
docker compose ps
docker compose logs -f cue-agent
curl http://localhost:8080/healthz
```

The `/healthz` response includes:

- `providers` (up/down/unknown for each LLM provider)
- `loop.running`
- `loop.last_iteration_time`
- queued message count

Optional web dashboard:

- Enable with `CUE_DASHBOARD_ENABLED=true`
- Protect with `CUE_DASHBOARD_USERNAME` + `CUE_DASHBOARD_PASSWORD`
- Access at `http://localhost:8080/dashboard`

Audit retention:

- Configure `CUE_AUDIT_RETENTION_DAYS` and `CUE_AUDIT_CLEANUP_CRON` for daily cleanup of old audit rows.

### Persistence

`docker-compose.yml` mounts:

- `./data -> /data` for `cue_state.db`
- `./skills -> /app/skills` for hot-reloadable skills
- `./SOUL.md -> /app/SOUL.md` for agent identity/rules

## 2) Docker Compose Webhook + Automatic SSL

Use the webhook override stack when you want Telegram webhooks instead of polling and automatic HTTPS termination via Caddy.

### Prerequisites

- Public DNS record pointing to your host (`WEBHOOK_DOMAIN`)
- Ports `80/tcp` and `443/tcp` open to the internet
- Telegram webhook URL configured in `.env.production`

### Steps

1. Set webhook environment values in `.env.production`:
   - `CUE_RUN_MODE=webhook`
   - `CUE_TELEGRAM_WEBHOOK_URL=https://<domain>/telegram/webhook`
   - `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN=<long-random-token>`
2. Start webhook stack:

```bash
WEBHOOK_DOMAIN=bot.example.com \
docker compose -f docker-compose.yml -f docker-compose.webhook.yml up -d --build
```

### Verify

```bash
curl https://bot.example.com/healthz
```

Check webhook diagnostics in health payload:

- `telegram.mode`
- `telegram.webhook.registered`
- `telegram.webhook.request_count`
- `telegram.webhook.rejected_count`
- `telegram.webhook.last_error`

### TLS for local development (self-signed)

```bash
./scripts/generate-self-signed-cert.sh ./certs localhost 365
```

## 3) systemd (Linux VM/Bare Metal)

### Setup

```bash
sudo useradd --system --create-home cueagent
sudo mkdir -p /opt/cue-agent
sudo chown -R cueagent:cueagent /opt/cue-agent
```

Deploy app code to `/opt/cue-agent`, create venv, and install:

```bash
cd /opt/cue-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.production.example .env
```

Edit `/opt/cue-agent/.env`.

### Service file

Create `/etc/systemd/system/cue-agent.service`:

```ini
[Unit]
Description=CueAgent service
After=network.target

[Service]
Type=simple
User=cueagent
Group=cueagent
WorkingDirectory=/opt/cue-agent
EnvironmentFile=/opt/cue-agent/.env
ExecStart=/opt/cue-agent/.venv/bin/cue-agent --mode polling
Restart=always
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cue-agent
sudo systemctl start cue-agent
sudo systemctl status cue-agent
```

## 4) Cloud Examples

### Railway (Dockerfile deploy)

1. Create a new Railway project from this repo.
2. Railway auto-detects the `Dockerfile`.
3. Add variables from `.env.production.example` in Railway Variables.
4. Set persistent volume mount path to `/data`.
5. Deploy and confirm health endpoint `/healthz` is reachable.

### Fly.io

1. Install `flyctl` and run `fly launch --dockerfile Dockerfile`.
2. Configure secrets: `fly secrets set CUE_TELEGRAM_BOT_TOKEN=... CUE_OPENAI_API_KEY=...`.
3. Add volume for state:
   - `fly volumes create cue_data --size 1 --region <region>`
   - mount to `/data` in `fly.toml`
4. Expose internal port `8080` for health checks.
5. Deploy with `fly deploy`.

### DigitalOcean App Platform

1. Create a new app from GitHub repository.
2. Choose Dockerfile source.
3. Configure environment variables from `.env.production.example`.
4. Add persistent storage and mount it at `/data`.
5. Set health check path to `/healthz`.
6. Deploy and validate logs plus Telegram connectivity.

## Operations Notes

- Graceful shutdown: CueAgent handles `SIGTERM`, stops loop/heartbeat/telegram cleanly, then exits.
- Restart policy: use `restart: unless-stopped` (Docker) or `Restart=always` (systemd).

### Backup and runbook

- **State database** — Periodically back up `./data/cue_state.db` (or `/data/cue_state.db` in Docker). This holds conversation state, task queue, audit trail, and user/role data.
- **Suggested runbook** — (1) Stop or quiesce the app if you need a consistent snapshot. (2) Copy `./data/cue_state.db` to a dated backup path or remote storage. (3) If using vector memory, also back up `./data/vector_memory` (or the path set by `CUE_VECTOR_MEMORY_PATH`). (4) Restore by replacing the file and restarting the service.
- **Retention** — Align backup frequency with `CUE_AUDIT_RETENTION_DAYS`; keep at least one backup beyond your retention window.

### Secrets rotation

- **`.env` / `.env.production`** — Rotate in place: update the token/key, then restart the process (Docker: `docker compose up -d --force-recreate`; systemd: `sudo systemctl restart cue-agent`). Avoid leaving old values in history or logs.
- **Telegram bot token** — Create a new bot with [@BotFather](https://t.me/BotFather) if needed; update `CUE_TELEGRAM_BOT_TOKEN` and restart. For webhook mode, re-register the webhook URL after rotating.
- **Webhook secret** — Generate a new random value for `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN` and update the running config; no need to change the bot token.
- **LLM API keys** — Rotate in the provider’s dashboard, then update the corresponding `CUE_*_API_KEY` and restart. No in-app key history.

### Health and dashboard in production

- **`/healthz`** — Use for liveness/readiness probes. Response includes `providers` (LLM provider status), `loop.running`, `loop.last_iteration_time`, and (in webhook mode) `telegram.webhook.*` diagnostics. Bind with `CUE_HEALTHCHECK_HOST` / `CUE_HEALTHCHECK_PORT` (default `0.0.0.0:8080`).
- **Dashboard** — Enable with `CUE_DASHBOARD_ENABLED=true`. Routes: `/dashboard` (summary), `/dashboard/actions`, `/dashboard/tasks`, `/dashboard/providers`. Protected by HTTP Basic Auth (`CUE_DASHBOARD_USERNAME`, `CUE_DASHBOARD_PASSWORD`). Set strong credentials and restrict access (firewall, VPN, or reverse proxy auth) when exposed. Timeline length is limited by `CUE_DASHBOARD_TIMELINE_LIMIT`.
- **Tuning** — In high-load or multi-instance setups, run only one instance with the loop enabled, or use external task distribution; the in-app task queue is single-process.

## Proxy and Firewall Checklist

- Allow inbound `443/tcp` (and `80/tcp` for ACME challenges/redirects).
- Restrict direct access to webhook listener port (`8081`) so only the reverse proxy can reach it.
- Ensure reverse proxy forwards:
  - `POST /telegram/webhook` -> `cue-agent:8081`
  - health/dashboard paths -> `cue-agent:8080`
- If using a cloud load balancer, preserve TLS at edge and route only HTTPS traffic to webhook endpoint.

## Polling to Webhook Migration

1. Set `CUE_RUN_MODE=webhook`.
2. Configure:
   - `CUE_TELEGRAM_WEBHOOK_URL`
   - `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN`
   - `CUE_TELEGRAM_WEBHOOK_LISTEN_HOST/PORT/PATH`
3. Deploy proxy/SSL (for example `docker-compose.webhook.yml` + Caddy).
4. Confirm `/healthz` shows webhook diagnostics and `registered=true`.
5. Send a Telegram message and verify `telegram.webhook.request_count` increments.

## Telegram group topics (threaded replies)

In groups that have **topics** (forum-style threads) enabled, the bot can reply in the same thread as the user’s message so conversation history stays in one place.

- **Behavior** — When an inbound message has a topic/thread ID (`message_thread_id`), the gateway uses that ID for all replies (text and documents) in that chat. So the reply appears in the same topic thread.
- **Configuration** — Set `CUE_TELEGRAM_USE_TOPIC_REPLIES=true` (default) to enable. Set to `false` to always reply in the main chat (useful for groups that do not use topics).
- **Detection** — Thread ID is read from the Telegram message when present (forum/topic chats only) and passed through to send calls; no per-chat configuration is required.
