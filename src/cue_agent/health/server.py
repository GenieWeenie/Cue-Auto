"""Async HTTP server for health probes, optional monitoring dashboard, and /metrics."""

from __future__ import annotations

import asyncio
import base64
import time
import binascii
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from html import escape
from secrets import compare_digest
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_READ_LIMIT = 4096
_READ_TIMEOUT_SECONDS = 2.0
_DASHBOARD_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #13232f;
background: linear-gradient(180deg, #f8fbf6 0%, #edf4ee 100%); }
header { background: #103321; color: #f2f7f2; padding: 14px 18px; border-bottom: 3px solid #3f8f53; }
h1 { margin: 0; font-size: 20px; letter-spacing: 0.2px; }
h2 { margin: 0 0 10px 0; font-size: 18px; }
main { padding: 18px; max-width: 1200px; margin: 0 auto; }
nav a { display: inline-block; margin: 0 10px 10px 0; padding: 8px 10px; text-decoration: none;
background: #1f5c39; color: #fff; border-radius: 8px; font-weight: 600; font-size: 14px; }
.meta { color: #5c6f64; font-size: 13px; margin-top: 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.card { background: #ffffff; border: 1px solid #d4dfd7; border-radius: 10px; padding: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.ok { color: #24773b; font-weight: 700; }
.warn { color: #955f00; font-weight: 700; }
.bad { color: #9a1f1f; font-weight: 700; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d4dfd7; border-radius: 10px; overflow: hidden; }
th, td { text-align: left; border-bottom: 1px solid #e7ece8; padding: 8px; font-size: 13px; vertical-align: top; }
th { background: #f1f5f2; font-size: 12px; text-transform: uppercase; color: #4a6053; letter-spacing: 0.3px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
"""


class HealthServer:
    """Serve JSON health checks and an optional basic-auth dashboard."""

    def __init__(
        self,
        host: str,
        port: int,
        status_provider: Callable[[], dict[str, Any]],
        *,
        dashboard_enabled: bool = False,
        dashboard_status_provider: Callable[[], dict[str, Any]] | None = None,
        dashboard_username: str = "admin",
        dashboard_password: str = "change-me",
        metrics_enabled: bool = False,
        metrics_provider: Callable[[], bytes] | None = None,
        metrics_record_request: Callable[[str, float], None] | None = None,
    ):
        self.host = host
        self.port = port
        self._status_provider = status_provider
        self._dashboard_enabled = dashboard_enabled
        self._dashboard_provider = dashboard_status_provider
        self._dashboard_username = dashboard_username
        self._dashboard_password = dashboard_password
        self._metrics_enabled = metrics_enabled
        self._metrics_provider = metrics_provider
        self._metrics_record_request = metrics_record_request
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, host=self.host, port=self.port)
        logger.info(
            "Health endpoint started",
            extra={
                "event": "health_server_started",
                "host": self.host,
                "port": self.bound_port,
                "dashboard_enabled": self._dashboard_enabled,
            },
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
        path = ""
        start = time.monotonic()
        try:
            raw = await asyncio.wait_for(reader.read(_READ_LIMIT), timeout=_READ_TIMEOUT_SECONDS)
            method, path, headers = self._parse_request(raw)

            if method == "GET" and path == "/metrics":
                if self._metrics_enabled and self._metrics_provider is not None:
                    body = self._metrics_provider()
                    await self._write_response(
                        writer,
                        status="200 OK",
                        body=body,
                        content_type="text/plain; charset=utf-8; version=0.0.4",
                    )
                else:
                    await self._write_json(writer, status="404 Not Found", payload={"error": "metrics_disabled"})
                return

            if method == "GET" and path in {"/healthz", "/health", "/"}:
                await self._write_json(writer, status="200 OK", payload=self._status_provider())
                return

            if method == "GET" and path.startswith("/dashboard"):
                await self._handle_dashboard_request(writer, path, headers)
                return

            await self._write_json(writer, status="404 Not Found", payload={"error": "not_found"})
        except Exception:
            logger.exception("Health server request failed")
            try:
                await self._write_json(writer, status="500 Internal Server Error", payload={"error": "internal_error"})
            except Exception:
                pass
        finally:
            if path != "/metrics" and self._metrics_record_request is not None:
                self._metrics_record_request(path or "/", time.monotonic() - start)
            writer.close()
            await writer.wait_closed()

    def _parse_request(self, raw: bytes) -> tuple[str, str, dict[str, str]]:
        lines = raw.split(b"\r\n")
        request_line = lines[0].decode("utf-8", errors="replace") if lines else ""
        parts = request_line.split(" ")
        method = parts[0] if len(parts) >= 1 else ""
        target = parts[1] if len(parts) >= 2 else ""
        path = urlsplit(target).path or "/"

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            if ":" not in decoded:
                continue
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return method, path, headers

    async def _handle_dashboard_request(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        headers: dict[str, str],
    ) -> None:
        if not self._dashboard_enabled or self._dashboard_provider is None:
            await self._write_json(writer, status="404 Not Found", payload={"error": "dashboard_disabled"})
            return
        if not self._is_dashboard_authorized(headers.get("authorization", "")):
            await self._write_response(
                writer,
                status="401 Unauthorized",
                body=b"Dashboard authentication required.",
                content_type="text/plain; charset=utf-8",
                extra_headers={"WWW-Authenticate": 'Basic realm="CueAgent Dashboard"'},
            )
            return

        snapshot = self._dashboard_provider()
        if path in {"/dashboard", "/dashboard/"}:
            await self._write_html(writer, status="200 OK", html=self._render_home(snapshot))
            return
        if path == "/dashboard/actions":
            await self._write_html(writer, status="200 OK", html=self._render_actions(snapshot))
            return
        if path == "/dashboard/tasks":
            await self._write_html(writer, status="200 OK", html=self._render_tasks(snapshot))
            return
        if path == "/dashboard/providers":
            await self._write_html(writer, status="200 OK", html=self._render_providers(snapshot))
            return
        if path == "/dashboard/api/summary":
            await self._write_json(writer, status="200 OK", payload=snapshot)
            return
        if path == "/dashboard/api/actions":
            await self._write_json(writer, status="200 OK", payload={"actions": snapshot.get("actions", [])})
            return
        if path == "/dashboard/api/tasks":
            await self._write_json(
                writer,
                status="200 OK",
                payload={
                    "queue": snapshot.get("queue", {}),
                    "tasks": snapshot.get("tasks", []),
                },
            )
            return
        if path == "/dashboard/api/providers":
            await self._write_json(
                writer,
                status="200 OK",
                payload={
                    "providers": snapshot.get("providers", {}),
                    "provider_metrics": snapshot.get("provider_metrics", {}),
                },
            )
            return
        await self._write_json(writer, status="404 Not Found", payload={"error": "not_found"})

    def _is_dashboard_authorized(self, authorization_header: str) -> bool:
        scheme, _, encoded = authorization_header.partition(" ")
        if scheme.lower() != "basic" or not encoded.strip():
            return False
        try:
            decoded = base64.b64decode(encoded.strip()).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        expected = f"{self._dashboard_username}:{self._dashboard_password}"
        return compare_digest(decoded, expected)

    def _render_home(self, snapshot: dict[str, Any]) -> str:
        runtime = snapshot.get("runtime", {})
        providers = snapshot.get("providers", {})
        agents = snapshot.get("agents", {})
        workflows = snapshot.get("workflows", {})
        queue = snapshot.get("queue", {})
        queue_stats = queue.get("task_queue", {}) if isinstance(queue, dict) else {}
        provider_lines = self._provider_badges(providers)
        recent_errors_html = self._recent_errors_card(snapshot.get("recent_errors", [])[:5])
        return self._wrap_dashboard_html(
            title="CueAgent Dashboard",
            subtitle="Home",
            body=(
                "<section class='grid'>"
                f"{self._card('Runtime', self._runtime_lines(runtime))}"
                f"{self._card('Current Task', self._text_or_none(runtime.get('current_task')))}"
                f"{self._card('Provider Health', provider_lines)}"
                f"{self._card('Multi-Agent', self._agent_lines(agents))}"
                f"{self._card('Workflows', self._workflow_lines(workflows))}"
                f"{self._card('Task Queue', self._queue_lines(queue_stats))}"
                f"{recent_errors_html}"
                "</section>"
            ),
        )

    def _render_actions(self, snapshot: dict[str, Any]) -> str:
        rows = snapshot.get("actions", [])
        if not isinstance(rows, list):
            rows = []
        table_rows: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            table_rows.append(
                (
                    "<tr>"
                    f"<td><code>{escape(str(row.get('timestamp_utc', '')))}</code></td>"
                    f"<td><code>{escape(str(row.get('tool_name', '-')))}</code></td>"
                    f"<td>{escape(str(row.get('risk_level', '-')))}</td>"
                    f"<td>{escape(str(row.get('duration_ms', '-')))}</td>"
                    f"<td>{escape(str(row.get('outcome', '-')))}</td>"
                    f"<td>{escape(str(row.get('summary', '')))}</td>"
                    "</tr>"
                )
            )
        body_rows = "".join(table_rows) or "<tr><td colspan='6'>No action timeline data yet.</td></tr>"
        body = (
            "<table><thead><tr>"
            "<th>Time (UTC)</th><th>Tool/Event</th><th>Risk</th><th>Duration (ms)</th><th>Outcome</th><th>Summary</th>"
            f"</tr></thead><tbody>{body_rows}</tbody></table>"
        )
        return self._wrap_dashboard_html(title="CueAgent Dashboard", subtitle="Action Timeline", body=body)

    def _render_tasks(self, snapshot: dict[str, Any]) -> str:
        tasks = snapshot.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        table_rows: list[str] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = escape(str(task.get("id", "")))
            title = escape(str(task.get("title", "")))
            status = escape(str(task.get("status", "")))
            priority = escape(str(task.get("priority", "")))
            deps = task.get("depends_on", [])
            deps_text = escape(str(deps)) if isinstance(deps, list) else escape(str(deps))
            table_rows.append(
                (
                    "<tr>"
                    f"<td><code>#{task_id}</code></td>"
                    f"<td>{title}</td>"
                    f"<td>{status}</td>"
                    f"<td>{priority}</td>"
                    f"<td>{deps_text}</td>"
                    "</tr>"
                )
            )
        queue = snapshot.get("queue", {})
        queue_stats = queue.get("task_queue", {}) if isinstance(queue, dict) else {}
        rows_html = "".join(table_rows) or "<tr><td colspan='5'>No tasks.</td></tr>"
        body = (
            f"<div class='card'>{self._queue_lines(queue_stats)}</div>"
            "<div style='height:12px'></div>"
            "<table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Priority</th><th>Depends On</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
        )
        return self._wrap_dashboard_html(title="CueAgent Dashboard", subtitle="Task Queue", body=body)

    def _render_providers(self, snapshot: dict[str, Any]) -> str:
        statuses = snapshot.get("providers", {})
        metrics = snapshot.get("provider_metrics", {})
        if not isinstance(statuses, dict):
            statuses = {}
        if not isinstance(metrics, dict):
            metrics = {}
        names = sorted({*statuses.keys(), *metrics.keys()})
        rows: list[str] = []
        for name in names:
            metric = metrics.get(name, {})
            if not isinstance(metric, dict):
                metric = {}
            rows.append(
                (
                    "<tr>"
                    f"<td><code>{escape(str(name))}</code></td>"
                    f"<td>{escape(str(statuses.get(name, 'unknown')))}</td>"
                    f"<td>{escape(str(metric.get('requests', 0)))}</td>"
                    f"<td>{escape(str(metric.get('avg_latency_ms', 0)))}</td>"
                    f"<td>{escape(str(metric.get('estimated_cost_usd', 0.0)))}</td>"
                    f"<td>{escape(str(metric.get('last_model', 'n/a')))}</td>"
                    "</tr>"
                )
            )
        rows_html = "".join(rows) or "<tr><td colspan='6'>No provider metrics.</td></tr>"
        body = (
            "<table><thead><tr>"
            "<th>Provider</th><th>Status</th><th>Requests</th><th>Avg Latency (ms)</th><th>Estimated Cost (USD)</th><th>Last Model</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table>"
        )
        return self._wrap_dashboard_html(title="CueAgent Dashboard", subtitle="Provider Health", body=body)

    def _wrap_dashboard_html(self, *, title: str, subtitle: str, body: str) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<meta http-equiv='refresh' content='15'>"
            f"<title>{escape(title)} - {escape(subtitle)}</title>"
            f"<style>{_DASHBOARD_CSS}</style></head><body>"
            f"<header><h1>{escape(title)}</h1></header><main>"
            "<nav>"
            "<a href='/dashboard'>Home</a>"
            "<a href='/dashboard/actions'>Action Timeline</a>"
            "<a href='/dashboard/tasks'>Task Queue</a>"
            "<a href='/dashboard/providers'>Provider Health</a>"
            "</nav>"
            f"<h2>{escape(subtitle)}</h2>"
            f"<div class='meta'>UTC now: <code>{escape(timestamp)}</code> · Auto-refresh every 15s</div>"
            "<div style='height:12px'></div>"
            f"{body}"
            "</main></body></html>"
        )

    def _provider_badges(self, providers: Any) -> str:
        if not isinstance(providers, dict) or not providers:
            return "No provider data."
        lines: list[str] = []
        for name in sorted(providers.keys()):
            status = str(providers.get(name, "unknown"))
            css = "warn"
            if status == "up":
                css = "ok"
            elif status == "down":
                css = "bad"
            lines.append(f"<div><code>{escape(name)}</code>: <span class='{css}'>{escape(status)}</span></div>")
        return "".join(lines)

    def _runtime_lines(self, runtime: Any) -> str:
        if not isinstance(runtime, dict):
            return "No runtime data."
        status = escape(str(runtime.get("status", "unknown")))
        uptime = escape(str(runtime.get("uptime_human", "n/a")))
        started = escape(str(runtime.get("started_at_utc", "n/a")))
        return (
            f"<div>Status: <span class='ok'>{status}</span></div>"
            f"<div>Uptime: <code>{uptime}</code></div>"
            f"<div>Started: <code>{started}</code></div>"
        )

    def _queue_lines(self, queue_stats: Any) -> str:
        if not isinstance(queue_stats, dict):
            return "No queue data."
        pieces = [f"{escape(str(k))}=<code>{escape(str(v))}</code>" for k, v in sorted(queue_stats.items())]
        return "<div>" + " · ".join(pieces) + "</div>"

    def _agent_lines(self, agents: Any) -> str:
        if not isinstance(agents, dict):
            return "No multi-agent data."
        return (
            "<div>"
            f"enabled=<code>{escape(str(agents.get('enabled', False)))}</code> · "
            f"active_parents=<code>{escape(str(agents.get('active_parents', 0)))}</code> · "
            f"active_sub_agents=<code>{escape(str(agents.get('active_sub_agents', 0)))}</code>"
            "</div>"
            "<div>"
            f"requests=<code>{escape(str(agents.get('subagent_requests', 0)))}</code> · "
            "subagent_cost_usd="
            f"<code>{escape(str(agents.get('subagent_estimated_cost_usd', 0.0)))}</code>"
            "</div>"
        )

    def _workflow_lines(self, workflows: Any) -> str:
        if not isinstance(workflows, dict):
            return "No workflow data."
        return (
            "<div>"
            f"enabled=<code>{escape(str(workflows.get('enabled', False)))}</code> · "
            f"loaded=<code>{escape(str(workflows.get('loaded', 0)))}</code> · "
            f"templates=<code>{escape(str(workflows.get('templates', 0)))}</code>"
            "</div>"
            "<div>"
            f"running_tasks=<code>{escape(str(workflows.get('running_tasks', 0)))}</code> · "
            f"hot_reload=<code>{escape(str(workflows.get('hot_reload', False)))}</code>"
            "</div>"
        )

    def _text_or_none(self, value: Any) -> str:
        text = str(value).strip() if value is not None else ""
        if not text:
            return "No active task."
        return f"<code>{escape(text)}</code>"

    def _recent_errors_card(self, recent_errors: list[Any]) -> str:
        """Render a card for recent_errors (list of dicts with timestamp_utc, message, outcome)."""
        if not isinstance(recent_errors, list):
            recent_errors = []
        items = [e for e in recent_errors if isinstance(e, dict)][:5]
        if not items:
            return self._card("Recent errors", "No recent errors.")
        rows: list[str] = []
        for row in items:
            ts = escape(str(row.get("timestamp_utc", "")))
            msg = escape(str(row.get("message", "")))
            outcome = escape(str(row.get("outcome", "")))
            rows.append(f"<tr><td><code>{ts}</code></td><td>{msg}</td><td>{outcome}</td></tr>")
        table = (
            "<table><thead><tr><th>Time (UTC)</th><th>Message</th><th>Outcome</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
        return self._card("Recent errors", table)

    def _card(self, title: str, content_html: str) -> str:
        return f"<div class='card'><h2>{escape(title)}</h2>{content_html}</div>"

    async def _write_json(self, writer: asyncio.StreamWriter, *, status: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        await self._write_response(writer, status=status, body=body, content_type="application/json; charset=utf-8")

    async def _write_html(self, writer: asyncio.StreamWriter, *, status: str, html: str) -> None:
        await self._write_response(
            writer,
            status=status,
            body=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        *,
        status: str,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = [
            f"HTTP/1.1 {status}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        for key, value in (extra_headers or {}).items():
            headers.append(f"{key}: {value}")
        headers.extend(["", ""])
        writer.write("\r\n".join(headers).encode("utf-8") + body)
        await writer.drain()
