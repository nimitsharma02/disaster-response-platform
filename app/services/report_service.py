"""Community reports: fetch, normalise, classify, cache, degrade."""

from __future__ import annotations

import asyncio
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import cache
from app.core.config import settings
from app.db.models.disaster import Disaster
from app.db.models.enums import ReportPriority
from app.integrations import official_client, social_client
from app.integrations.resilience import CircuitOpenError, CircuitState, get_breaker
from app.schemas.report import Report, ReportsMeta, ReportsResponse
from app.services import classification, disaster_service

log = structlog.get_logger(__name__)

_DEGRADED_TTL = 60


def _query_for(disaster: Disaster) -> str:
    """What to ask the providers about."""
    return (disaster.location_name or disaster.title).strip()


async def get_reports(
    session: AsyncSession,
    disaster_id: uuid.UUID,
    *,
    priority: ReportPriority | None = None,
    limit: int = 50,
    force_refresh: bool = False,
) -> ReportsResponse:
    disaster = await disaster_service.get_disaster(session, disaster_id)
    query = _query_for(disaster)

    key = cache.make_key("reports", str(disaster_id))
    sources: dict[str, str] = {}

    async def _load() -> dict[str, object]:
        reports, outcomes = await _fetch_all(query)
        return {"reports": [r.model_dump(mode="json") for r in reports], "sources": outcomes}

    result = await cache.get_or_set(
        key,
        _load,
        ttl=settings.cache_ttl_reports,
        stale_grace=settings.cache_stale_grace,
        force_refresh=force_refresh,
    )

    payload = result.value or {}
    raw_reports = payload.get("reports", []) if isinstance(payload, dict) else []
    sources = payload.get("sources", {}) if isinstance(payload, dict) else {}

    provider_degraded = any(state not in ("ok", "empty") for state in sources.values())
    degraded = result.degraded or provider_degraded

    if result.degraded:
        sources = _current_provider_states()

    if provider_degraded and result.status == "MISS":
        await cache.set(key, payload, ttl=_DEGRADED_TTL, stale_grace=settings.cache_stale_grace)

    reports = [Report.model_validate(r) for r in raw_reports]
    reports = [classification.classify_report(r) for r in reports]
    reports.sort(key=classification.sort_key)

    counts: dict[str, int] = {}
    for report in reports:
        counts[str(report.priority)] = counts.get(str(report.priority), 0) + 1

    if priority is not None:
        reports = [r for r in reports if r.priority == priority]

    total_fetched = len(reports)
    reports = reports[:limit]

    log.info(
        "reports_served",
        disaster_id=str(disaster_id),
        cache=result.status,
        degraded=degraded,
        count=len(reports),
        sources=sources,
    )

    return ReportsResponse(
        disaster_id=str(disaster_id),
        items=reports,
        meta=ReportsMeta(
            cache=result.status,
            age_seconds=round(result.age_seconds, 1),
            degraded=degraded,
            sources=sources,
            total_fetched=total_fetched,
            counts_by_priority=counts,
        ),
    )


def _current_provider_states() -> dict[str, str]:
    """Why each provider is unavailable right now."""
    states: dict[str, str] = {}
    for provider in (social_client.PROVIDER, official_client.PROVIDER):
        breaker = get_breaker(provider)
        states[provider] = (
            "circuit_open" if breaker.state is CircuitState.OPEN else "failed"
        )
    return states


async def _fetch_all(query: str) -> tuple[list[Report], dict[str, str]]:
    """Fetch both providers concurrently and merge what came back."""
    social_task = social_client.fetch_reports(query, limit=12)
    official_task = official_client.fetch_bulletins(query, limit=5)

    results = await asyncio.gather(social_task, official_task, return_exceptions=True)

    reports: list[Report] = []
    outcomes: dict[str, str] = {}
    failures = 0

    for provider, outcome in zip(
        (social_client.PROVIDER, official_client.PROVIDER), results, strict=True
    ):
        if isinstance(outcome, CircuitOpenError):
            outcomes[provider] = "circuit_open"
            failures += 1
        elif isinstance(outcome, BaseException):
            outcomes[provider] = "failed"
            failures += 1
            log.warning("report_provider_failed", provider=provider, error=str(outcome))
        else:
            outcomes[provider] = "ok" if outcome else "empty"
            reports.extend(outcome)

    if failures == len(results):
        raise RuntimeError(f"all report providers failed: {outcomes}")

    return reports, outcomes


async def invalidate_reports_cache(disaster_id: uuid.UUID) -> None:
    """Drop the cached reports for one incident."""
    await cache.invalidate(cache.make_key("reports", str(disaster_id)))
