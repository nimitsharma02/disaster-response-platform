"""Error model and global exception handlers."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger(__name__)

PROBLEM_JSON = "application/problem+json"
ERROR_DOC_BASE = "https://docs.drp.local/errors"


class AppError(Exception):
    """Base class for every error this application raises on purpose."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.errors = errors or []
        self.headers = headers or {}
        super().__init__(self.detail)

    def to_problem(self, instance: str, request_id: str | None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{ERROR_DOC_BASE}/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
            "code": self.code,
        }
        if request_id:
            problem["request_id"] = request_id
        if self.errors:
            problem["errors"] = self.errors
        return problem


class ValidationError(AppError):
    status_code = 422  # literal: Starlette renamed its 422 constant
    code = "validation_error"
    title = "Request validation failed"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    title = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    title = "Conflicting state"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    title = "Authentication required or invalid"

    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        kw.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kw)


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    title = "Not permitted"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    title = "Too many requests"


class UpstreamError(AppError):
    """An external dependency failed and no fallback was available."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_unavailable"
    title = "Upstream service unavailable"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    title = "Service temporarily unavailable"


# Handlers


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _respond(request: Request, exc: AppError) -> JSONResponse:
    rid = _request_id(request)
    headers = dict(exc.headers)
    if rid:
        headers["X-Request-ID"] = rid
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_problem(request.url.path, rid),
        media_type=PROBLEM_JSON,
        headers=headers,
    )


_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}

_TITLE_BY_STATUS = {
    400: "Bad request",
    401: "Authentication required or invalid",
    403: "Not permitted",
    404: "Resource not found",
    405: "Method not allowed",
    409: "Conflicting state",
    422: "Request validation failed",
    429: "Too many requests",
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger = log.warning if exc.status_code < 500 else log.error
        logger(
            "request_failed",
            code=exc.code,
            status=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
        )
        return _respond(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())),
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return _respond(
            request,
            ValidationError("One or more fields failed validation", errors=errors),
        )

    @app.exception_handler(PydanticValidationError)
    async def _pydantic(request: Request, exc: PydanticValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())),
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return _respond(
            request, ValidationError("One or more fields failed validation", errors=errors)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapped = AppError(str(exc.detail))
        mapped.status_code = exc.status_code
        mapped.code = _CODE_BY_STATUS.get(exc.status_code, "http_error")
        mapped.title = _TITLE_BY_STATUS.get(exc.status_code, "Request failed")
        mapped.headers = dict(getattr(exc, "headers", None) or {})
        return _respond(request, mapped)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path, error=str(exc))
        return _respond(
            request,
            AppError("An unexpected error occurred. Quote request_id when reporting this."),
        )
