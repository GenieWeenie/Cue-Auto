# syntax=docker/dockerfile:1

FROM python:3.12-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache git build-base

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip wheel --wheel-dir /wheels .


FROM python:3.12-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    CUE_STATE_DB_PATH=/data/cue_state.db \
    CUE_SKILLS_DIR=/app/skills \
    CUE_SOUL_MD_PATH=/app/SOUL.md \
    CUE_HEALTHCHECK_ENABLED=true \
    CUE_HEALTHCHECK_HOST=0.0.0.0 \
    CUE_HEALTHCHECK_PORT=8080

RUN apk add --no-cache tini ca-certificates

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels \
      efficient-agent-protocol \
      openai anthropic python-telegram-bot apscheduler pydantic-settings python-dotenv httpx \
    && python -m pip install --no-index --find-links=/wheels cue-agent --no-deps \
    && rm -rf /wheels

COPY SOUL.md /app/SOUL.md
COPY skills /app/skills
RUN mkdir -p /data \
    && addgroup -S cue \
    && adduser -S -u 10001 -G cue cue \
    && chown -R cue:cue /app /data

USER cue

EXPOSE 8080
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; p=os.getenv('CUE_HEALTHCHECK_PORT','8080'); urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3).read()" || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["cue-agent", "--mode", "polling"]
