"""Tests for the lightweight health HTTP endpoint."""

from __future__ import annotations

import asyncio
import json

import pytest

from cue_agent.health.server import HealthServer


async def _request(path: str, port: int) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("utf-8"))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()

    header, body = raw.split(b"\r\n\r\n", 1)
    status_line = header.splitlines()[0].decode("utf-8")
    status_code = int(status_line.split(" ")[1])
    return status_code, json.loads(body.decode("utf-8"))


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
        status_code, payload = await _request("/healthz", server.bound_port)
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
        status_code, payload = await _request("/nope", server.bound_port)
        assert status_code == 404
        assert payload["error"] == "not_found"
    finally:
        await server.stop()
