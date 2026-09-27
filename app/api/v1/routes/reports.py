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
    """Aggregated reports from two external providers."""
    result = await report_service.get_reports(
        session, disaster_id, priority=priority, limit=limit, force_refresh=refresh
    )

    response.headers["X-Cache"] = result.meta.cache
    response.headers["X-Cache-Age"] = str(result.meta.age_seconds)
    if result.meta.degraded:
        response.headers["X-Degraded"] = "true"
    return result
