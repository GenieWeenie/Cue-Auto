"""Tests for health endpoint and optional monitoring dashboard."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from cue_agent.health.server import HealthServer


async def _raw_request(
    path: str, port: int, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n{header_lines}\r\n"
    writer.write(request.encode("utf-8"))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()

    header_blob, body = raw.split(b"\r\n\r\n", 1)
    lines = header_blob.decode("utf-8").split("\r\n")
    status_code = int(lines[0].split(" ")[1])
    parsed_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed_headers[key.strip().lower()] = value.strip()
    return status_code, parsed_headers, body


async def _request_json(
    path: str, port: int, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], dict]:
    status, response_headers, body = await _raw_request(path, port, headers=headers)
    return status, response_headers, json.loads(body.decode("utf-8"))


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


@pytest.mark.asyncio
async def test_health_server_returns_json_payload():
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok", "providers": {"openai": "up"}},
    )
    await server.start()
    try:
        assert server.bound_port is not None
        status_code, _, payload = await _request_json("/healthz", server.bound_port)
        assert status_code == 200
        assert payload["status"] == "ok"
        assert payload["providers"]["openai"] == "up"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_health_server_returns_404_for_unknown_path():
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
    )
    await server.start()
    try:
        assert server.bound_port is not None
        status_code, _, payload = await _request_json("/nope", server.bound_port)
        assert status_code == 404
        assert payload["error"] == "not_found"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_requires_basic_auth():
    snapshot = {
        "runtime": {"status": "running", "uptime_human": "1m 0s", "started_at_utc": "2026-02-28T00:00:00+00:00"},
        "providers": {"openai": "up"},
        "queue": {"task_queue": {"pending": 1}},
    }
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=True,
        dashboard_status_provider=lambda: snapshot,
        dashboard_username="user",
        dashboard_password="pass",
    )
    await server.start()
    try:
        assert server.bound_port is not None
        status_code, headers, body = await _raw_request("/dashboard", server.bound_port)
        assert status_code == 401
        assert "basic" in headers["www-authenticate"].lower()
        assert b"authentication required" in body.lower()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_pages_and_json_routes_with_auth():
    snapshot = {
        "runtime": {
            "status": "running",
            "uptime_human": "3m 22s",
            "started_at_utc": "2026-02-28T00:00:00+00:00",
            "current_task": "Review queue",
        },
        "providers": {"openai": "up", "anthropic": "down"},
        "provider_metrics": {
            "openai": {"requests": 10, "avg_latency_ms": 300, "estimated_cost_usd": 0.12, "last_model": "gpt-4o"}
        },
        "queue": {"task_queue": {"pending": 2, "in_progress": 1, "total": 3}},
        "tasks": [{"id": 1, "title": "Do thing", "status": "pending", "priority": 2, "depends_on": []}],
        "actions": [
            {
                "timestamp_utc": "2026-02-28T00:01:00+00:00",
                "tool_name": "run_shell",
                "risk_level": "high",
                "duration_ms": 12,
                "outcome": "success",
                "summary": "cmd executed",
            }
        ],
    }
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=True,
        dashboard_status_provider=lambda: snapshot,
        dashboard_username="user",
        dashboard_password="pass",
    )
    await server.start()
    try:
        assert server.bound_port is not None
        auth = _basic_auth_header("user", "pass")

        started = time.perf_counter()
        status_code, _, body = await _raw_request("/dashboard", server.bound_port, headers=auth)
        elapsed = time.perf_counter() - started
        assert status_code == 200
        html = body.decode("utf-8")
        assert "CueAgent Dashboard" in html
        assert "Current Task" in html
        assert elapsed < 2.0

        status_code, _, body = await _raw_request("/dashboard/actions", server.bound_port, headers=auth)
        assert status_code == 200
        assert "Action Timeline" in body.decode("utf-8")

        status_code, _, payload = await _request_json("/dashboard/api/summary", server.bound_port, headers=auth)
        assert status_code == 200
        assert payload["runtime"]["status"] == "running"

        status_code, _, payload = await _request_json("/dashboard/api/providers", server.bound_port, headers=auth)
        assert status_code == 200
        assert payload["providers"]["openai"] == "up"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_healthz_health_and_root_return_same_shape():
    """GET /healthz, /health, and / return 200 with status_provider payload."""
    payload = {"status": "ok", "version": "test"}
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: payload,
    )
    await server.start()
    try:
        assert server.bound_port is not None
        for path in ["/healthz", "/health", "/"]:
            status_code, _, body = await _request_json(path, server.bound_port)
            assert status_code == 200
            assert body["status"] == "ok"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_disabled_returns_404():
    """When dashboard_enabled=False, GET /dashboard returns 404 dashboard_disabled."""
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=False,
    )
    await server.start()
    try:
        assert server.bound_port is not None
        status_code, _, body = await _request_json("/dashboard", server.bound_port)
        assert status_code == 404
        assert body["error"] == "dashboard_disabled"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_wrong_password_returns_401():
    """Wrong Basic credentials return 401."""
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=True,
        dashboard_status_provider=lambda: {"runtime": {}},
        dashboard_username="u",
        dashboard_password="p",
    )
    await server.start()
    try:
        assert server.bound_port is not None
        wrong_auth = _basic_auth_header("u", "wrong")
        status_code, _, body = await _raw_request("/dashboard", server.bound_port, headers=wrong_auth)
        assert status_code == 401
        assert b"authentication required" in body.lower()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_tasks_and_providers_pages():
    """GET /dashboard/tasks and /dashboard/providers return HTML."""
    snapshot = {
        "runtime": {},
        "providers": {"openai": "up"},
        "provider_metrics": {},
        "queue": {"task_queue": {}},
        "tasks": [],
    }
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=True,
        dashboard_status_provider=lambda: snapshot,
        dashboard_username="a",
        dashboard_password="b",
    )
    await server.start()
    try:
        assert server.bound_port is not None
        auth = _basic_auth_header("a", "b")
        status_code, _, body = await _raw_request("/dashboard/tasks", server.bound_port, headers=auth)
        assert status_code == 200
        assert b"Task Queue" in body and b"CueAgent Dashboard" in body
        status_code, _, body = await _raw_request("/dashboard/providers", server.bound_port, headers=auth)
        assert status_code == 200
        assert b"Provider Health" in body
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_api_actions_and_tasks():
    """GET /dashboard/api/actions and /dashboard/api/tasks return JSON."""
    snapshot = {
        "actions": [{"tool_name": "run_shell", "outcome": "success"}],
        "tasks": [{"id": 1, "title": "T1", "status": "pending"}],
        "queue": {"task_queue": {"pending": 1}},
    }
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=True,
        dashboard_status_provider=lambda: snapshot,
        dashboard_username="x",
        dashboard_password="y",
    )
    await server.start()
    try:
        assert server.bound_port is not None
        auth = _basic_auth_header("x", "y")
        status_code, _, payload = await _request_json("/dashboard/api/actions", server.bound_port, headers=auth)
        assert status_code == 200
        assert "actions" in payload
        assert len(payload["actions"]) == 1
        status_code, _, payload = await _request_json("/dashboard/api/tasks", server.bound_port, headers=auth)
        assert status_code == 200
        assert "tasks" in payload
        assert payload["queue"] == snapshot["queue"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_dashboard_unknown_path_returns_404():
    """GET /dashboard/unknown returns 404 not_found."""
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
        dashboard_enabled=True,
        dashboard_status_provider=lambda: {},
        dashboard_username="u",
        dashboard_password="p",
    )
    await server.start()
    try:
        assert server.bound_port is not None
        auth = _basic_auth_header("u", "p")
        status_code, _, payload = await _request_json("/dashboard/unknown", server.bound_port, headers=auth)
        assert status_code == 404
        assert payload["error"] == "not_found"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_health_server_start_idempotent():
    """Calling start() twice does not fail."""
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"status": "ok"},
    )
    await server.start()
    await server.start()
    try:
        assert server.bound_port is not None
        status_code, _, _ = await _request_json("/healthz", server.bound_port)
        assert status_code == 200
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_health_server_status_provider_raises_returns_500():
    """When status_provider raises, response is 500 internal_error."""

    def _raise():
        raise RuntimeError("diagnostics failed")

    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=_raise,
    )
    await server.start()
    try:
        assert server.bound_port is not None
        status_code, _, payload = await _request_json("/healthz", server.bound_port)
        assert status_code == 500
        assert payload["error"] == "internal_error"
    finally:
        await server.stop()
