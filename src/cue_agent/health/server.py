"""Minimal async HTTP health endpoint server."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_READ_LIMIT = 4096
_READ_TIMEOUT_SECONDS = 2.0


class HealthServer:
    """Serve a simple JSON health endpoint for container probes."""

    def __init__(self, host: str, port: int, status_provider: Callable[[], dict[str, Any]]):
        self.host = host
        self.port = port
        self._status_provider = status_provider
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, host=self.host, port=self.port)
        logger.info(
            "Health endpoint started",
            extra={"event": "health_server_started", "host": self.host, "port": self.bound_port},
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("Health endpoint stopped", extra={"event": "health_server_stopped"})

    @property
    def bound_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(_READ_LIMIT), timeout=_READ_TIMEOUT_SECONDS)
            request_line = raw.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            parts = request_line.split(" ")
            method = parts[0] if len(parts) >= 1 else ""
            path = parts[1] if len(parts) >= 2 else ""

            if method == "GET" and path in {"/healthz", "/health", "/"}:
                payload = self._status_provider()
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                await self._write_response(writer, status="200 OK", body=body)
            else:
                await self._write_response(writer, status="404 Not Found", body=b'{"error":"not_found"}')
        except Exception:
            logger.exception("Health server request failed")
            try:
                await self._write_response(
                    writer,
                    status="500 Internal Server Error",
                    body=b'{"error":"internal_error"}',
                )
            except Exception:
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _write_response(writer: asyncio.StreamWriter, *, status: str, body: bytes) -> None:
        headers = [
            f"HTTP/1.1 {status}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "",
            "",
        ]
        writer.write("\r\n".join(headers).encode("utf-8") + body)
        await writer.drain()
