"""Shared outbound HTTP client.

One ``httpx.AsyncClient`` for the whole process, created at startup and closed at
shutdown. Creating a client per call would throw away the connection pool and pay
a fresh TCP + TLS handshake every time - on a hot path that dominates the actual
request latency.

This module's other job is to translate transport-level reality into the two
categories ``resilience.py`` reasons about:

    timeout / connection error / 5xx / 429  ->  TransientError  (retry, trip breaker)
    4xx (except 429)                        ->  PermanentError  (never retry)

Timeouts are **always** explicit. httpx defaults to 5s but a client with no
timeout at all is the single most common way an async service dies: one hung
upstream socket holds a connection, then a worker, then all of them.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config import settings
from app.integrations.resilience import PermanentError, TransientError

log = structlog.get_logger(__name__)

_client: httpx.AsyncClient | None = None


def build_timeout() -> httpx.Timeout:
    """Separate connect and read budgets.

    They fail for different reasons: connect failing means the host is
    unreachable (fail fast), read failing means it accepted us but is slow
    (allow longer). Collapsing them into one number loses that distinction.
    """
    return httpx.Timeout(
        connect=settings.http_connect_timeout_s,
        read=settings.http_read_timeout_s,
        write=settings.http_read_timeout_s,
        pool=settings.http_connect_timeout_s,
    )


async def init_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=build_timeout(),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        log.info("http_client_ready")
    return _client


def get_http_client() -> httpx.AsyncClient:
    """Lazily create outside app lifespan (tests, scripts, the arq worker)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=build_timeout(), follow_redirects=True)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test hook for injecting a MockTransport-backed client."""
    global _client
    _client = client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | float | None = None,
) -> Any:
    """Perform one HTTP call and return parsed JSON, or raise Transient/Permanent.

    No retry and no breaker here on purpose - this is a single attempt. Layering
    is the caller's decision, because whether a call is safe to retry depends on
    whether it is idempotent, and only the caller knows that.
    """
    client = get_http_client()
    try:
        response = await client.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=timeout if timeout is not None else build_timeout(),
        )
    except httpx.TimeoutException as exc:
        raise TransientError(f"{provider}: request timed out ({exc.__class__.__name__})") from exc
    except httpx.TransportError as exc:
        # DNS failure, connection refused, TLS error, connection reset.
        raise TransientError(f"{provider}: transport error: {exc}") from exc

    status = response.status_code

    if status >= 500:
        raise TransientError(f"{provider}: upstream returned {status}")
    if status == 429:
        # We are being throttled. Transient by definition - backing off is
        # exactly the correct response.
        raise TransientError(f"{provider}: rate limited (429)")
    if status >= 400:
        raise PermanentError(f"{provider}: client error {status}: {response.text[:200]}")

    try:
        return response.json()
    except ValueError as exc:
        # A 200 carrying HTML (a captive portal, an error page) is a broken
        # upstream, not a broken request - so transient, and worth one retry.
        raise TransientError(f"{provider}: response was not valid JSON") from exc
