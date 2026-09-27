"""Structured logging.

Logs are events with fields, not sentences with values glued into them. Every
line carries the ``request_id`` of the request that produced it, bound once in
middleware via a ``ContextVar`` so no function has to thread it through its
signature. ``LOG_FORMAT=json`` emits machine-parseable output for a log shipper;
``console`` is the readable local default.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

from app.core.config import settings

# Bound by RequestContextMiddleware; read by the structlog processor below.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def _inject_request_id(_logger: object, _name: str, event_dict: dict) -> dict:
    rid = request_id_ctx.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging() -> None:
    """Route stdlib logging and structlog through one shared pipeline."""
    level = getattr(logging, settings.log_level, logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _inject_request_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # stdlib LoggerFactory, not PrintLoggerFactory: the `add_logger_name`
        # processor reads `logger.name`, which only a stdlib logger has. It also
        # means output from libraries that log through stdlib lands in the same
        # stream with the same handler.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # uvicorn.access duplicates our own access log line, so silence it and keep ours.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpcore", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
