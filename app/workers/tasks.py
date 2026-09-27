"""Background jobs (bonus feature).

The worker's only job is **cache pre-warming**: it refreshes the report cache for
active incidents so an operator opening a disaster gets a cache hit instead of
waiting on two external calls.

The important property is that it is strictly optional. Nothing in the request
path depends on the worker running - a cold cache simply takes the normal miss
path. The worker makes the system faster, never correct. That is deliberate:
a background job that the API depends on is a second thing that can take the API
down.

Jobs are enqueued by the API on write, and a cron re-warms the top active
incidents periodically so a long-lived incident stays warm.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.db.session import get_sessionmaker
from app.repositories import disaster_repo
from app.services import report_service

log = structlog.get_logger(__name__)


async def refresh_reports(ctx: dict[str, Any], disaster_id: str) -> dict[str, Any]:
    """Re-fetch and re-cache the reports for one incident."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            result = await report_service.get_reports(
                session, uuid.UUID(disaster_id), force_refresh=True, limit=200
            )
        except Exception as exc:  # noqa: BLE001
            # A pre-warm failure is not an incident. The next real request will
            # take the normal miss path and surface any genuine problem there.
            log.warning("cache_prewarm_failed", disaster_id=disaster_id, error=str(exc))
            return {"disaster_id": disaster_id, "ok": False, "error": str(exc)}

    log.info(
        "cache_prewarmed",
        disaster_id=disaster_id,
        reports=len(result.items),
        degraded=result.meta.degraded,
    )
    return {
        "disaster_id": disaster_id,
        "ok": True,
        "reports": len(result.items),
        "degraded": result.meta.degraded,
    }


async def prewarm_active_disasters(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron job: refresh reports for incidents still being responded to.

    Only active incidents (reported / verified / in_progress) are refreshed.
    Re-fetching reports for a resolved incident would spend the external call
    budget on data nobody is going to look at.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        # Bounded so the job cannot grow without limit as the dataset does.
        disaster_ids = await disaster_repo.list_active_ids(session, limit=25)

    refreshed = 0
    for disaster_id in disaster_ids:
        outcome = await refresh_reports(ctx, str(disaster_id))
        refreshed += int(bool(outcome.get("ok")))

    log.info("prewarm_cycle_complete", considered=len(disaster_ids), refreshed=refreshed)
    return {"considered": len(disaster_ids), "refreshed": refreshed}
