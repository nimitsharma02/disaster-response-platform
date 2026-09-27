"""Disaster CRUD endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import (
    CurrentUser,
    DbSession,
    Pagination,
    RequestId,
    RequireAdmin,
    RequireWriter,
)
from app.db.models.enums import DisasterStatus
from app.schemas.common import Page, ProblemDetail
from app.schemas.disaster import (
    DisasterCreate,
    DisasterListFilters,
    DisasterResponse,
    DisasterUpdate,
)
from app.services import disaster_service, report_service

router = APIRouter(prefix="/disasters", tags=["Disasters"])

_ERRORS = {
    401: {"model": ProblemDetail, "description": "Missing or invalid token"},
    403: {"model": ProblemDetail, "description": "Role or ownership check failed"},
    404: {"model": ProblemDetail, "description": "Disaster not found"},
    422: {"model": ProblemDetail, "description": "Validation failed"},
}


@router.post(
    "",
    response_model=DisasterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a disaster",
    responses=_ERRORS,
)
async def create_disaster(
    payload: DisasterCreate,
    session: DbSession,
    actor: RequireWriter,
    request_id: RequestId,
) -> DisasterResponse:
    """Create an incident."""
    disaster = await disaster_service.create_disaster(
        session, payload, actor, request_id=request_id
    )
    return disaster_service.to_response(disaster)


@router.get(
    "",
    response_model=Page[DisasterResponse],
    summary="List disasters with filters",
    responses={422: {"model": ProblemDetail}},
)
async def list_disasters(
    session: DbSession,
    pagination: Pagination,
    tag: Annotated[str | None, Query(description="Exact tag match, e.g. `?tag=flood`")] = None,
    status_filter: Annotated[
        DisasterStatus | None,
        Query(alias="status", description="Filter by lifecycle status"),
    ] = None,
    q: Annotated[
        str | None,
        Query(min_length=2, max_length=100, description="Substring of title/description"),
    ] = None,
    created_by: Annotated[uuid.UUID | None, Query(description="Filter by creator id")] = None,
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
    radius_km: Annotated[
        float | None, Query(gt=0, le=20_000, description="Requires lat and lng")
    ] = None,
    sort: Annotated[
        str, Query(description="created_at | -created_at | title | -title | distance")
    ] = "-created_at",
) -> Page[DisasterResponse]:
    """List incidents."""
    filters = DisasterListFilters(
        tag=tag, status=status_filter, q=q, created_by=created_by,
        lat=lat, lng=lng, radius_km=radius_km, sort=sort,
    )
    return await disaster_service.list_disasters(
        session, filters, limit=pagination.limit, offset=pagination.offset
    )


@router.get(
    "/{disaster_id}",
    response_model=DisasterResponse,
    summary="Fetch one disaster",
    responses={404: {"model": ProblemDetail}},
)
async def get_disaster(disaster_id: uuid.UUID, session: DbSession) -> DisasterResponse:
    disaster = await disaster_service.get_disaster(session, disaster_id)
    return disaster_service.to_response(disaster)


@router.patch(
    "/{disaster_id}",
    response_model=DisasterResponse,
    summary="Partially update a disaster",
    responses=_ERRORS,
)
async def update_disaster(
    disaster_id: uuid.UUID,
    payload: DisasterUpdate,
    session: DbSession,
    actor: RequireWriter,
    request_id: RequestId,
) -> DisasterResponse:
    """Update an incident."""
    disaster = await disaster_service.update_disaster(
        session, disaster_id, payload, actor, request_id=request_id
    )
    if {"description", "location_name", "latitude", "title"} & payload.model_fields_set:
        await report_service.invalidate_reports_cache(disaster_id)
    return disaster_service.to_response(disaster)


@router.delete(
    "/{disaster_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a disaster (admin only)",
    responses=_ERRORS,
)
async def delete_disaster(
    disaster_id: uuid.UUID,
    session: DbSession,
    actor: RequireAdmin,
    request_id: RequestId,
) -> Response:
    """Delete an incident."""
    await disaster_service.delete_disaster(session, disaster_id, actor, request_id=request_id)
    await report_service.invalidate_reports_cache(disaster_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{disaster_id}/audit",
    summary="Audit trail for a disaster",
    responses={401: {"model": ProblemDetail}, 404: {"model": ProblemDetail}},
)
async def get_audit_trail(
    disaster_id: uuid.UUID,
    session: DbSession,
    actor: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[dict]:
    """Who changed this incident, what they changed, and when."""
    from app.repositories import audit_repo

    await disaster_service.get_disaster(session, disaster_id)
    entries = await audit_repo.list_for_entity(
        session, entity_type="disaster", entity_id=disaster_id, limit=limit
    )
    return [
        {
            "id": str(e.id),
            "action": str(e.action),
            "actor": e.actor_username,
            "changes": e.changes,
            "request_id": e.request_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
