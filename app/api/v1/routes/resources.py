"""Resource endpoints, including the geospatial nearby search."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.config import settings
from app.core.deps import DbSession, Pagination, RequestId, RequireAdmin
from app.db.models.enums import ResourceType
from app.schemas.common import Page, PageMeta, ProblemDetail
from app.schemas.resource import (
    NearbySearchParams,
    NearbySearchResponse,
    ResourceCreate,
    ResourceResponse,
)
from app.services import resource_service

router = APIRouter(tags=["Resources"])


@router.get(
    "/disasters/{disaster_id}/resources",
    response_model=NearbySearchResponse,
    summary="Find resources near a disaster",
    responses={
        404: {"model": ProblemDetail, "description": "Disaster not found"},
        422: {"model": ProblemDetail, "description": "Invalid parameters, or the disaster "
                                                     "has no location and none was supplied"},
    },
)
async def find_nearby_resources(
    disaster_id: uuid.UUID,
    session: DbSession,
    lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Search centre latitude. Defaults to the disaster's own."),
    ] = None,
    lng: Annotated[
        float | None,
        Query(ge=-180, le=180, description="Search centre longitude."),
    ] = None,
    radius_km: Annotated[
        float,
        # The cap is enforced here, at the framework boundary, so an over-large
        # radius is a clean 422 and the limit appears in the OpenAPI schema. The
        # same rule is repeated in NearbySearchParams as defence in depth for
        # non-HTTP callers (the worker, scripts).
        Query(gt=0, le=settings.max_radius_km,
              description=f"Search radius in km (max {settings.max_radius_km})"),
    ] = settings.default_radius_km,
    type: Annotated[  # noqa: A002 - matches the documented query parameter name
        ResourceType | None, Query(description="Restrict to one resource type")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NearbySearchResponse:
    """Resources within ``radius_km`` of a point, nearest first.

    **How the search works.** The radius filter is a PostGIS
    ``ST_DWithin(location, point, radius_km * 1000)`` against a
    ``geography(Point, 4326)`` column with a **GiST index**. ``ST_DWithin`` is
    index-accelerated: the planner uses the index to discard rows by bounding
    box first and computes exact distances only on the survivors.
    ``ST_Distance(...) < r`` would look equivalent and force a full table scan.

    Distances come back from the database in kilometres, and ordering happens in
    SQL so ``LIMIT`` returns the genuinely nearest rows rather than the first N
    matches re-sorted in Python.

    **Search centre.** Supply ``lat``/``lng`` to search around an arbitrary
    point (a responder's own position). Omit them and the disaster's own resolved
    location is used. If the disaster has no location and none is given, the
    response is a 422 - "we do not know where to look" is not the same answer as
    "there is nothing nearby".

    **Radius cap.** ``radius_km`` is capped at ``MAX_RADIUS_KM`` so one request
    cannot be used to scan the whole table.
    """
    params = NearbySearchParams(lat=lat, lng=lng, radius_km=radius_km, type=type, limit=limit)
    return await resource_service.find_nearby_for_disaster(session, disaster_id, params)


@router.get(
    "/resources",
    response_model=Page[ResourceResponse],
    summary="List resources",
)
async def list_resources(
    session: DbSession,
    pagination: Pagination,
    type: Annotated[ResourceType | None, Query()] = None,  # noqa: A002
    disaster_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[ResourceResponse]:
    """Plain, non-spatial listing. Useful for seeding a map before a search."""
    items, total = await resource_service.list_resources(
        session,
        resource_type=type,
        disaster_id=disaster_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page(
        items=items,
        meta=PageMeta(total=total, limit=pagination.limit, offset=pagination.offset),
    )


@router.post(
    "/resources",
    response_model=ResourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a resource (admin only)",
    responses={
        401: {"model": ProblemDetail},
        403: {"model": ProblemDetail},
        404: {"model": ProblemDetail, "description": "Linked disaster not found"},
        422: {"model": ProblemDetail},
    },
)
async def create_resource(
    payload: ResourceCreate,
    session: DbSession,
    actor: RequireAdmin,
    request_id: RequestId,
) -> ResourceResponse:
    """Register a resource.

    **Authorization:** ``admin`` only. Resources are reference data that the
    whole response depends on - a wrong shelter location sends people to the
    wrong place - so writes are deliberately narrower than for incidents.

    Coordinates are required here, unlike on a disaster: a resource with no
    position can never be returned by a proximity search.
    """
    resource = await resource_service.create_resource(
        session, payload, actor, request_id=request_id
    )
    return resource_service.to_response(resource)


@router.get(
    "/resources/{resource_id}",
    response_model=ResourceResponse,
    summary="Fetch one resource",
    responses={404: {"model": ProblemDetail}},
)
async def get_resource(resource_id: uuid.UUID, session: DbSession) -> ResourceResponse:
    resource = await resource_service.get_resource(session, resource_id)
    return resource_service.to_response(resource)
