# CueAgent Deployment Guide

This guide covers production deployment options for CueAgent:

1. Docker Compose (recommended)
2. systemd on a Linux VM
3. Cloud deploy examples (Railway, Fly.io, DigitalOcean App Platform)

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

## 2) systemd (Linux VM/Bare Metal)

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

## 3) Cloud Examples

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
