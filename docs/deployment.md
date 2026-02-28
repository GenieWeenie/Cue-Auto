# CueAgent Deployment Guide

This guide covers production deployment options for CueAgent:

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
- Backups: periodically back up `./data/cue_state.db`.
- Keep dashboard credentials rotated when exposing dashboard routes beyond localhost.
- Keep `CUE_TELEGRAM_WEBHOOK_SECRET_TOKEN` rotated and never reuse bot tokens as webhook secrets.

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
