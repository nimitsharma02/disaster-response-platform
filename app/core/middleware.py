"""HTTP middleware: request context, access logging, security headers, rate limiting.

Order matters and is set in ``app/main.py``. Starlette runs middleware in reverse
registration order on the way in, so the request-context middleware is registered
last in order to run first - every other layer, including the rate limiter's
rejection, needs a request id to attach to its log line and response.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.cache.backend import get_backend
from app.core.config import settings
from app.core.errors import PROBLEM_JSON, RateLimitError
from app.core.logging import request_id_ctx

log = structlog.get_logger("app.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, time the request, and emit one access log line.

    An inbound ``X-Request-ID`` is honoured so a trace can be followed across
    services; otherwise one is generated. It is echoed back on the response and
    included in every error body, which is what makes "quote the request id"
    actually useful when someone reports a problem.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers build the response; this only records
            # timing and re-raises so they can do their job.
            duration_ms = (time.perf_counter() - started) * 1000
            log.error(
                "request_errored",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise
        finally:
            request_id_ctx.reset(token)

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"

        user = getattr(request.state, "user", None)
        log.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
            actor=getattr(user, "username", None),
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers.

    Modest for a JSON API, but free. ``default-src 'none'`` is the right default
    for JSON, but it would block the pages that legitimately load assets - the
    Swagger/ReDoc UIs and the demo map client, which pull Leaflet from a CDN.
    Those paths are exempted rather than weakening the policy everywhere.
    """

    _ASSET_PATHS = ("/docs", "/redoc", "/openapi.json", "/demo")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")

        if not request.url.path.startswith(self._ASSET_PATHS):
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'")

        # HSTS only makes sense once traffic is actually HTTPS; asserting it in
        # local development would just poison the developer's browser.
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting, keyed by client IP.

    Backed by Redis ``INCR`` + ``EXPIRE`` so the budget is shared across workers;
    with the in-memory fallback it becomes per-process, which is noted as a
    limitation rather than pretended away.

    Fixed window, not sliding: it is one atomic operation per request instead of
    a sorted-set of timestamps. The known flaw is burst tolerance at a window
    boundary (up to 2x the limit across two adjacent windows). For abuse
    dampening on an internal-facing API that is an acceptable trade; a public
    API would want a sliding window or token bucket.

    Login gets a much tighter budget of its own, because that is the endpoint
    worth brute-forcing.
    """

    _EXEMPT = ("/api/v1/health", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.rate_limit_enabled or request.url.path.startswith(self._EXEMPT):
            return await call_next(request)

        # WebSocket and SSE connections are long-lived; counting them per
        # request would be meaningless.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        is_auth = request.url.path.endswith("/auth/login")
        limit = settings.auth_rate_limit_requests if is_auth else settings.rate_limit_requests
        window = settings.auth_rate_limit_window_s if is_auth else settings.rate_limit_window_s

        request_id = getattr(request.state, "request_id", None)
        client = request.client.host if request.client else "unknown"
        bucket = "auth" if is_auth else "api"
        # The window is part of the key, so a new window is a new counter and no
        # separate reset job is needed.
        key = f"drp:ratelimit:{bucket}:{client}:{int(time.time() // window)}"

        try:
            count = await get_backend().incr(key, ttl=window + 1)
        except Exception as exc:  # noqa: BLE001
            # Fail open. A rate limiter that takes the API down when its own
            # store is unavailable has caused a worse outage than the one it
            # was protecting against.
            log.warning("rate_limit_store_unavailable_failing_open", error=str(exc))
            return await call_next(request)

        if count > limit:
            retry_after = window - int(time.time() % window)
            log.warning(
                "rate_limit_exceeded",
                client=client,
                bucket=bucket,
                count=count,
                limit=limit,
                path=request.url.path,
            )
            # Built here rather than raised: registered exception handlers live
            # *inside* the routing layer, so an exception raised from middleware
            # would escape them and surface as a bare 500. Constructing the same
            # RFC 9457 body directly keeps the contract identical.
            error = RateLimitError(
                f"Rate limit exceeded: {limit} requests per {window}s. "
                f"Retry in {retry_after}s."
            )
            return JSONResponse(
                status_code=error.status_code,
                content=error.to_problem(request.url.path, request_id),
                media_type=PROBLEM_JSON,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-Request-ID": request_id or "",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response
