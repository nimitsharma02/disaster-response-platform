"""HTTP middleware: request context, access logging, security headers,."""

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
    """Assign a request id, time the request, and emit one access log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
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
    """Baseline hardening headers."""

    _ASSET_PATHS = ("/docs", "/redoc", "/openapi.json", "/demo")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")

        if not request.url.path.startswith(self._ASSET_PATHS):
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'")

        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting, keyed by client IP."""

    _EXEMPT = ("/api/v1/health", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.rate_limit_enabled or request.url.path.startswith(self._EXEMPT):
            return await call_next(request)

        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        is_auth = request.url.path.endswith("/auth/login")
        limit = settings.auth_rate_limit_requests if is_auth else settings.rate_limit_requests
        window = settings.auth_rate_limit_window_s if is_auth else settings.rate_limit_window_s

        request_id = getattr(request.state, "request_id", None)
        client = request.client.host if request.client else "unknown"
        bucket = "auth" if is_auth else "api"
        key = f"drp:ratelimit:{bucket}:{client}:{int(time.time() // window)}"

        try:
            count = await get_backend().incr(key, ttl=window + 1)
        except Exception as exc:  # noqa: BLE001
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
