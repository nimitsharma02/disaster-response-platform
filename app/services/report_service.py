"""Community reports: fetch, normalise, classify, cache, degrade.

Request flow::

    GET /disasters/{id}/reports
        |
        v
    build query from the disaster (location_name, else title)
        |
        v
    cache.get_or_set("reports:{id}")
        | HIT  -> return immediately, no external call at all
        | MISS -> loader:
        |            asyncio.gather(social, official)   <- concurrent, not serial
        |            at least one succeeded -> return what we have, note failures
        |            all failed             -> raise, so the cache serves stale
        v
    classify -> sort -> filter -> respond, with honest metadata

Points worth defending:

**Providers are fetched concurrently.** They are independent, so ``asyncio.gather``
makes the endpoint as slow as the slowest provider rather than the sum of both.
``return_exceptions=True`` is essential here: without it, one failure cancels the
sibling task and a working provider's data is thrown away.

**Partial failure is a first-class outcome.** One provider down still returns the
other's reports, with ``degraded: true`` and a per-provider ``sources`` map. A
partial result is cached with a **shortened TTL**, so we retry the broken
provider in a minute instead of serving a half-empty feed for the full window.

**Total failure returns stale data, not an error.** If both providers are down
and we have anything cached within the stale window, it is served with
``X-Cache: STALE`` and ``degraded: true``. Only a cold cache plus a dead upstream
produces a 502.

**The disaster's own record is never at risk.** These calls happen on the reports
endpoint alone. A dead social API cannot affect disaster CRUD.
"""

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

# A degraded payload is cached only briefly, so a recovering provider is picked
# up quickly rather than being masked for the full TTL.
_DEGRADED_TTL = 60


def _query_for(disaster: Disaster) -> str:
    """What to ask the providers about.

    The resolved place name is the best query when we have one; the title is the
    fallback, since it usually names the location too ("Manhattan Flash
    Flooding").
    """
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

    # A provider that failed on the way in makes this payload degraded, and so
    # does serving it from the stale window after an origin failure.
    provider_degraded = any(state not in ("ok", "empty") for state in sources.values())
    degraded = result.degraded or provider_degraded

    if result.degraded:
        # We are serving a cached payload because the live fetch failed. The
        # `sources` map stored alongside that payload describes the request that
        # originally populated it - very likely "ok, ok" - which would be an
        # actively misleading thing to report now. Replace it with why each
        # provider failed on THIS request.
        sources = _current_provider_states()

    # Shorten the cache life of a partial result so recovery is picked up fast.
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
    """Why each provider is unavailable right now.

    Read from the circuit breakers rather than from the cached payload, so a
    degraded response reports the live reason ("circuit_open" vs "failed")
    instead of the provenance of whatever was cached earlier.
    """
    states: dict[str, str] = {}
    for provider in (social_client.PROVIDER, official_client.PROVIDER):
        breaker = get_breaker(provider)
        states[provider] = (
            "circuit_open" if breaker.state is CircuitState.OPEN else "failed"
        )
    return states


async def _fetch_all(query: str) -> tuple[list[Report], dict[str, str]]:
    """Fetch both providers concurrently and merge what came back.

    Raises only when **every** provider failed, which is the signal the cache
    layer needs in order to fall back to stale data.
    """
    social_task = social_client.fetch_reports(query, limit=12)
    official_task = official_client.fetch_bulletins(query, limit=5)

    # return_exceptions=True: one provider failing must not cancel the other.
    results = await asyncio.gather(social_task, official_task, return_exceptions=True)

    reports: list[Report] = []
    outcomes: dict[str, str] = {}
    failures = 0

    for provider, outcome in zip(
        (social_client.PROVIDER, official_client.PROVIDER), results, strict=True
    ):
        if isinstance(outcome, CircuitOpenError):
            # Distinguished from a plain failure on purpose: it means we chose
            # not to call, which is useful when reading an incident afterwards.
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
        # Every provider is down. Raise so get_or_set can serve stale data.
        raise RuntimeError(f"all report providers failed: {outcomes}")

    return reports, outcomes


async def invalidate_reports_cache(disaster_id: uuid.UUID) -> None:
    """Drop the cached reports for one incident.

    Called when a disaster's description or location changes, because the
    provider query is derived from those fields - keeping the old payload would
    mean showing reports for the wrong place.
    """
    await cache.invalidate(cache.make_key("reports", str(disaster_id)))
