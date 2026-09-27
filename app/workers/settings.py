"""arq worker configuration."""

from __future__ import annotations

import structlog
from arq import cron
from arq.connections import RedisSettings

from app.cache.backend import close_backend, init_backend
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.integrations.http import close_http_client, init_http_client
from app.workers.tasks import prewarm_active_disasters, refresh_reports

configure_logging()
log = structlog.get_logger(__name__)


async def startup(ctx: dict) -> None:
    await init_backend()
    await init_http_client()
    log.info("worker_started")


async def shutdown(ctx: dict) -> None:
    await close_http_client()
    await close_backend()
    await dispose_engine()
    log.info("worker_stopped")


class WorkerSettings:
    functions = [refresh_reports]
    cron_jobs = [
        cron(prewarm_active_disasters, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown

    max_jobs = 5
    job_timeout = 60
    keep_result = 300
    max_tries = 3
