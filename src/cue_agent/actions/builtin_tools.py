"""Built-in tool implementations for CueAgent."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_SEARCH_PROVIDER_CHAIN = ("tavily", "serpapi", "duckduckgo")
_SEARCH_RATE_LOCK = threading.Lock()
_SEARCH_LAST_CALL_AT = 0.0


def _log_tool_execution(
    tool_name: str,
    risk_level: str,
    start_time: float,
    success: bool,
    error: str | None = None,
) -> None:
    duration_ms = int((time.monotonic() - start_time) * 1000)
    logger.info(
        "Tool execution",
        extra={
            "event": "tool_execution",
            "tool_name": tool_name,
            "risk_level": risk_level,
            "duration_ms": duration_ms,
            "success": success,
            "error": error,
        },
    )


def send_telegram(chat_id: str, text: str, *, bot: Any = None) -> dict[str, Any]:
    """Send a Telegram message. Bot instance injected via functools.partial."""
    start = time.monotonic()
    if bot is None:
        _log_tool_execution("send_telegram", "high", start, success=False, error="Telegram bot not configured")
        return {"error": "Telegram bot not configured"}
    import asyncio

    async def _send() -> dict[str, str]:
        attempts = max(1, int(os.getenv("CUE_RETRY_TELEGRAM_ATTEMPTS", "5")))
        base_delay = float(os.getenv("CUE_RETRY_BASE_DELAY_SECONDS", "0.5"))
        max_delay = float(os.getenv("CUE_RETRY_MAX_DELAY_SECONDS", "5.0"))
        jitter = float(os.getenv("CUE_RETRY_JITTER_SECONDS", "0.2"))

        from cue_agent.retry_utils import backoff_delay_seconds

        for attempt in range(1, attempts + 1):
            try:
                await bot.send_message(chat_id=int(chat_id), text=text)
                return {"status": "sent", "chat_id": chat_id}
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                status_code = getattr(exc, "status_code", None)
                message = str(exc).lower()
                retryable = (
                    retry_after is not None
                    or status_code == 429
                    or "timed out" in message
                    or "network" in message
                    or "timeout" in message
                )

                if not retryable or attempt >= attempts:
                    raise

                if retry_after is not None:
                    delay = float(retry_after)
                else:
                    delay = backoff_delay_seconds(
                        attempt,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        jitter=jitter,
                    )
                await asyncio.sleep(delay)
        raise RuntimeError("Telegram send retries exhausted")

    try:
        result = cast(dict[str, Any], asyncio.get_event_loop().run_until_complete(_send()))
    except Exception as exc:
        _log_tool_execution("send_telegram", "high", start, success=False, error=str(exc))
        return {"error": str(exc)}
    _log_tool_execution("send_telegram", "high", start, success=True)
    return result


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path.endswith("/") and path != "/":
        path = path[:-1]
    return f"{scheme}://{netloc}{path}"


def _tokenize(text: str) -> list[str]:
    return [token.strip().lower() for token in text.split() if token.strip()]


def _relevance_score(query: str, title: str, snippet: str, provider: str) -> float:
    tokens = _tokenize(query)
    haystack = f"{title} {snippet}".lower()
    token_hits = sum(1 for token in tokens if token in haystack)
    provider_bonus = {"tavily": 0.3, "serpapi": 0.2, "duckduckgo": 0.1}.get(provider, 0.0)
    return token_hits + provider_bonus


def _dedupe_and_rank_results(query: str, results: list[dict[str, str]], max_results: int) -> list[dict[str, str]]:
    deduped: dict[str, dict[str, str]] = {}
    for result in results:
        url = result.get("url", "")
        title = result.get("title", "")
        if not url or not title:
            continue

        normalized = _normalize_url(url)
        score = _relevance_score(query, title, result.get("snippet", ""), result.get("provider", ""))
        existing = deduped.get(normalized)
        if existing is None or score > float(existing.get("_score", 0.0)):
            enriched = dict(result)
            enriched["_score"] = f"{score:.3f}"
            enriched["_normalized_url"] = normalized
            deduped[normalized] = enriched

    ranked = sorted(
        deduped.values(),
        key=lambda row: float(row.get("_score", 0.0)),
        reverse=True,
    )

    output: list[dict[str, str]] = []
    for row in ranked[: max(1, max_results)]:
        clean = {k: v for k, v in row.items() if not k.startswith("_")}
        output.append(clean)
    return output


def _search_request_json(
    *,
    method: str,
    url: str,
    timeout_seconds: float,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.request(
            method=method,
            url=url,
            params=params,
            json=json_payload,
            headers=headers,
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return payload


def _search_tavily(query: str, max_results: int, include_content: bool) -> list[dict[str, str]]:
    api_key = os.getenv("CUE_TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("CUE_TAVILY_API_KEY not configured")

    payload = _search_request_json(
        method="POST",
        url="https://api.tavily.com/search",
        timeout_seconds=15.0,
        json_payload={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_images": False,
        },
    )
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        return []

    parsed: list[dict[str, str]] = []
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        url = str(row.get("url", "")).strip()
        snippet = str(row.get("content", "")).strip()
        if not title or not url:
            continue
        result: dict[str, str] = {
            "title": title,
            "url": url,
            "snippet": snippet,
            "provider": "tavily",
        }
        if include_content:
            result["content"] = snippet
        parsed.append(result)
    return parsed


def _region_to_serp_params(region: str) -> tuple[str, str]:
    cleaned = region.strip().lower()
    if "-" in cleaned:
        parts = cleaned.split("-", 1)
        gl = parts[0] or "us"
        hl = parts[1] or "en"
        return gl, hl
    if "_" in cleaned:
        parts = cleaned.split("_", 1)
        gl = parts[0] or "us"
        hl = parts[1] or "en"
        return gl, hl
    return cleaned or "us", "en"


def _search_serpapi(query: str, max_results: int, region: str) -> list[dict[str, str]]:
    api_key = os.getenv("CUE_SERPAPI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("CUE_SERPAPI_API_KEY not configured")

    gl, hl = _region_to_serp_params(region)
    payload = _search_request_json(
        method="GET",
        url="https://serpapi.com/search.json",
        timeout_seconds=15.0,
        params={
            "engine": "google",
            "q": query,
            "api_key": api_key,
            "num": max_results,
            "gl": gl,
            "hl": hl,
        },
    )
    raw_results = payload.get("organic_results", [])
    if not isinstance(raw_results, list):
        return []

    parsed: list[dict[str, str]] = []
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        url = str(row.get("link", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        if not title or not url:
            continue
        parsed.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "provider": "serpapi",
            }
        )
    return parsed


def _flatten_ddg_topics(rows: list[Any]) -> list[dict[str, str]]:
    flattened: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "Topics" in row and isinstance(row["Topics"], list):
            flattened.extend(_flatten_ddg_topics(row["Topics"]))
            continue
        text = str(row.get("Text", "")).strip()
        url = str(row.get("FirstURL", "")).strip()
        if text and url:
            title = text.split(" - ", 1)[0].strip()
            flattened.append({"title": title or text, "url": url, "snippet": text, "provider": "duckduckgo"})
    return flattened


def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    payload = _search_request_json(
        method="GET",
        url="https://api.duckduckgo.com/",
        timeout_seconds=12.0,
        params={
            "q": query,
            "format": "json",
            "no_redirect": 1,
            "no_html": 1,
            "skip_disambig": 1,
        },
    )

    parsed: list[dict[str, str]] = []
    abstract_url = str(payload.get("AbstractURL", "")).strip()
    abstract_text = str(payload.get("AbstractText", "")).strip()
    heading = str(payload.get("Heading", "")).strip()
    if abstract_url and abstract_text:
        parsed.append(
            {
                "title": heading or abstract_text[:80],
                "url": abstract_url,
                "snippet": abstract_text,
                "provider": "duckduckgo",
            }
        )

    related = payload.get("RelatedTopics", [])
    if isinstance(related, list):
        parsed.extend(_flatten_ddg_topics(related))

    return parsed[: max(1, max_results)]


def _search_provider_chain(preferred_provider: str) -> list[str]:
    provider = preferred_provider.strip().lower()
    if provider and provider != "auto":
        if "," in provider:
            chain = [p.strip() for p in provider.split(",") if p.strip()]
            return chain or list(_SEARCH_PROVIDER_CHAIN)
        return [provider]
    return list(_SEARCH_PROVIDER_CHAIN)


def _apply_search_rate_limit() -> None:
    global _SEARCH_LAST_CALL_AT  # noqa: PLW0603

    minimum_spacing = float(os.getenv("CUE_SEARCH_RATE_LIMIT_SECONDS", "1.0"))
    if minimum_spacing <= 0:
        return

    with _SEARCH_RATE_LOCK:
        now = time.monotonic()
        wait_seconds = minimum_spacing - (now - _SEARCH_LAST_CALL_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _SEARCH_LAST_CALL_AT = time.monotonic()


def web_search(
    query: str,
    max_results: int | None = None,
    region: str | None = None,
    provider: str | None = None,
    include_content: bool = False,
) -> dict[str, Any]:
    """Perform web search using provider fallback chain with dedupe and ranking."""
    start = time.monotonic()
    trimmed_query = query.strip()
    if not trimmed_query:
        _log_tool_execution("web_search", "low", start, success=False, error="Empty query")
        return {"error": "Query must not be empty"}

    configured_max_results = int(os.getenv("CUE_SEARCH_MAX_RESULTS", "5"))
    effective_max_results = max(1, max_results or configured_max_results)
    raw_region = region if region is not None else (os.getenv("CUE_SEARCH_REGION", "us-en") or "us-en")
    raw_provider = provider if provider is not None else (os.getenv("CUE_SEARCH_PROVIDER", "auto") or "auto")
    effective_region = raw_region.strip()
    effective_provider = raw_provider.strip()
    provider_chain = _search_provider_chain(effective_provider)

    _apply_search_rate_limit()

    aggregated: list[dict[str, str]] = []
    attempted: list[str] = []
    errors: dict[str, str] = {}
    provider_used = ""

    for candidate in provider_chain:
        attempted.append(candidate)
        try:
            if candidate == "tavily":
                results = _search_tavily(trimmed_query, effective_max_results, include_content=include_content)
            elif candidate == "serpapi":
                results = _search_serpapi(trimmed_query, effective_max_results, region=effective_region)
            elif candidate == "duckduckgo":
                results = _search_duckduckgo(trimmed_query, effective_max_results)
            else:
                errors[candidate] = "Unknown provider"
                continue
        except Exception as exc:
            errors[candidate] = str(exc)
            continue

        if results:
            aggregated.extend(results)
            provider_used = candidate
            break

    ranked_results = _dedupe_and_rank_results(trimmed_query, aggregated, max_results=effective_max_results)
    if not ranked_results:
        no_results: dict[str, Any] = {
            "query": trimmed_query,
            "results": [],
            "providers_attempted": attempted,
            "provider_used": None,
            "region": effective_region,
            "errors": errors,
            "note": "No search results available from configured providers.",
        }
        _log_tool_execution("web_search", "low", start, success=False, error="No results")
        return no_results

    result: dict[str, Any] = {
        "query": trimmed_query,
        "results": ranked_results,
        "providers_attempted": attempted,
        "provider_used": provider_used,
        "region": effective_region,
    }
    if errors:
        result["errors"] = errors
    _log_tool_execution("web_search", "low", start, success=True)
    return result


def read_file(path: str) -> dict[str, Any]:
    """Read a file from the workspace."""
    start = time.monotonic()
    target = Path(path)
    if not target.exists():
        _log_tool_execution("read_file", "low", start, success=False, error=f"File not found: {path}")
        return {"error": f"File not found: {path}"}
    try:
        content = target.read_text(encoding="utf-8")
        result = {"path": path, "content": content, "size_bytes": len(content)}
        _log_tool_execution("read_file", "low", start, success=True)
        return result
    except Exception as e:
        _log_tool_execution("read_file", "low", start, success=False, error=str(e))
        return {"error": str(e)}


def write_file(path: str, content: str) -> dict[str, Any]:
    """Write content to a file in the workspace."""
    start = time.monotonic()
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result = {"path": path, "size_bytes": len(content), "status": "written"}
        _log_tool_execution("write_file", "high", start, success=True)
        return result
    except Exception as e:
        _log_tool_execution("write_file", "high", start, success=False, error=str(e))
        return {"error": str(e)}


def run_shell(command: str, timeout: int = 30) -> dict[str, Any]:
    """Execute a shell command with timeout."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload = {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:2000],
        }
        _log_tool_execution("run_shell", "high", start, success=result.returncode == 0)
        return payload
    except subprocess.TimeoutExpired:
        _log_tool_execution("run_shell", "high", start, success=False, error=f"Timed out after {timeout}s")
        return {"command": command, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        _log_tool_execution("run_shell", "high", start, success=False, error=str(e))
        return {"command": command, "error": str(e)}
