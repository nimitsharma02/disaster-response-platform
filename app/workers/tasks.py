"""Background jobs (bonus feature)."""

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
    """Cron job: refresh reports for incidents still being responded to."""
    factory = get_sessionmaker()
    async with factory() as session:
        disaster_ids = await disaster_repo.list_active_ids(session, limit=25)

    refreshed = 0
    for disaster_id in disaster_ids:
        outcome = await refresh_reports(ctx, str(disaster_id))
        refreshed += int(bool(outcome.get("ok")))

    log.info("prewarm_cycle_complete", considered=len(disaster_ids), refreshed=refreshed)
    return {"considered": len(disaster_ids), "refreshed": refreshed}
