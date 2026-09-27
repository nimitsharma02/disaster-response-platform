"""arq worker configuration.

Run with::

    arq app.workers.settings.WorkerSettings

arq was chosen over Celery because it is async-native (our jobs are I/O-bound
HTTP calls, so they share the same event-loop model as the API), it needs only
Redis, and it is small enough to read end to end. Celery brings a much larger
surface and a synchronous worker model we would have to bridge.
"""

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
        # Every 5 minutes. Matches CACHE_TTL_REPORTS, so an entry is refreshed at
        # roughly the moment it would otherwise go stale.
        cron(prewarm_active_disasters, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown

    # Jobs are all outbound HTTP, so concurrency is cheap - but keep it modest so
    # a pre-warm burst cannot crowd out the API's own calls to the same provider.
    max_jobs = 5
    job_timeout = 60
    keep_result = 300
    # Retry a couple of times; beyond that the provider is genuinely down and the
    # circuit breaker is the right mechanism, not the queue.
    max_tries = 3
