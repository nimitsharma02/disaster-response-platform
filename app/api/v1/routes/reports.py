"""Community reports endpoint (external integration + caching)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core.deps import DbSession
from app.db.models.enums import ReportPriority
from app.schemas.common import ProblemDetail
from app.schemas.report import ReportsResponse
from app.services import report_service

router = APIRouter(prefix="/disasters", tags=["Reports"])


@router.get(
    "/{disaster_id}/reports",
    response_model=ReportsResponse,
    summary="Community and official reports for a disaster",
    responses={
        404: {"model": ProblemDetail, "description": "Disaster not found"},
        502: {"model": ProblemDetail, "description": "All providers failed and nothing was cached"},
    },
)
async def get_reports(
    disaster_id: uuid.UUID,
    session: DbSession,
    response: Response,
    priority: Annotated[
        ReportPriority | None, Query(description="Return only reports in this priority bucket")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    refresh: Annotated[
        bool, Query(description="Bypass the cache and re-fetch from the providers")
    ] = False,
) -> ReportsResponse:
    """Aggregated reports from two external providers.

    **Sources.** A mock social-media feed and a mock official bulletin feed, each
    running as a separate service behind a real HTTP boundary. Their payload
    shapes differ and are normalised into one ``Report`` model, so adding or
    swapping a provider changes only its client.

    **Caching.** Responses are cached for ``CACHE_TTL_REPORTS`` (default 5
    minutes) in Redis, fronted by a short in-process tier. The ``X-Cache`` header
    and ``meta.cache`` report ``HIT``, ``MISS`` or ``STALE``, and
    ``meta.age_seconds`` gives the age of the underlying data. Pass
    ``?refresh=true`` to force a re-fetch.

    **Failure handling.** Providers are called concurrently, and each one's
    outcome appears in ``meta.sources`` as ``ok``, ``empty``, ``failed`` or
    ``circuit_open``:

    * one provider down -> the other's reports are still returned,
      ``meta.degraded = true``, and the partial result is cached only briefly
      so recovery is picked up quickly;
    * both down, something cached -> the cached payload is served with
      ``X-Cache: STALE`` and ``meta.degraded = true``. A responder is far better
      served by five-minute-old reports than by an error;
    * both down, nothing cached -> 502, the only case where this endpoint fails.

    **Prioritisation.** Each report is scored by a deterministic, explainable
    classifier into ``critical``/``high``/``medium``/``low``, with
    ``matched_signals`` listing the rules that fired. Results are sorted most
    urgent first.
    """
    result = await report_service.get_reports(
        session, disaster_id, priority=priority, limit=limit, force_refresh=refresh
    )

    # Surfaced as headers too, so cache behaviour is visible in curl -D- without
    # having to parse the body.
    response.headers["X-Cache"] = result.meta.cache
    response.headers["X-Cache-Age"] = str(result.meta.age_seconds)
    if result.meta.degraded:
        response.headers["X-Degraded"] = "true"
    return result
