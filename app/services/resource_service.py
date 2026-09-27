"""Resource business logic, including the nearby-resources use case."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.db.models.enums import AuditAction, ResourceType
from app.db.models.resource import Resource
from app.realtime import events
from app.repositories import audit_repo, resource_repo
from app.repositories.geo import point_to_lat_lng
from app.schemas.auth import AuthenticatedUser
from app.schemas.common import GeoPoint
from app.schemas.resource import (
    NearbySearchParams,
    NearbySearchResponse,
    ResourceCreate,
    ResourceResponse,
)
from app.services import disaster_service

log = structlog.get_logger(__name__)

ENTITY = "resource"


async def find_nearby_for_disaster(
    session: AsyncSession,
    disaster_id: uuid.UUID,
    params: NearbySearchParams,
) -> NearbySearchResponse:
    """Resources near an incident.

    The search centre is resolved in this order:

    1. ``lat``/``lng`` from the query string, when supplied - a responder
       searching around *their own* position.
    2. Otherwise the disaster's own resolved location - the common case.

    If neither exists the request is a 422, not an empty list. Returning ``[]``
    would be a quiet lie: "no resources near here" and "we do not know where
    here is" are different answers, and only one of them is actionable.
    """
    disaster = await disaster_service.get_disaster(session, disaster_id)

    if params.lat is not None and params.lng is not None:
        center_lat, center_lng, center_source = params.lat, params.lng, "query"
    else:
        coords = point_to_lat_lng(disaster.location)
        if coords is None:
            raise ValidationError(
                "This disaster has no resolved location, so a search centre cannot be "
                "inferred. Supply lat and lng explicitly, or PATCH the disaster with "
                "coordinates first.",
                errors=[
                    {"field": "query.lat", "message": "required when the disaster has no location",
                     "type": "missing"},
                    {"field": "query.lng", "message": "required when the disaster has no location",
                     "type": "missing"},
                ],
            )
        center_lat, center_lng = coords
        center_source = "disaster"

    rows = await resource_repo.find_nearby(
        session,
        lat=center_lat,
        lng=center_lng,
        radius_km=params.radius_km,
        resource_type=params.type,
        disaster_id=disaster_id,
        include_unlinked=True,
        limit=params.limit,
    )

    log.info(
        "nearby_resources_searched",
        disaster_id=str(disaster_id),
        radius_km=params.radius_km,
        center_source=center_source,
        results=len(rows),
    )

    return NearbySearchResponse(
        center=GeoPoint(latitude=center_lat, longitude=center_lng),
        center_source=center_source,
        radius_km=params.radius_km,
        count=len(rows),
        items=[to_response(row.resource, distance_km=row.distance_km) for row in rows],
    )


async def list_resources(
    session: AsyncSession,
    *,
    resource_type: ResourceType | None,
    disaster_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[ResourceResponse], int]:
    rows, total = await resource_repo.list_resources(
        session,
        resource_type=resource_type,
        disaster_id=disaster_id,
        limit=limit,
        offset=offset,
    )
    return [to_response(r) for r in rows], total


async def create_resource(
    session: AsyncSession,
    payload: ResourceCreate,
    actor: AuthenticatedUser,
    *,
    request_id: str | None = None,
) -> Resource:
    # Validate the optional link before writing, so a bad disaster_id produces a
    # clear 404 rather than a foreign-key violation surfacing as a 500.
    if payload.disaster_id is not None:
        await disaster_service.get_disaster(session, payload.disaster_id)

    resource = await resource_repo.create(
        session,
        name=payload.name,
        resource_type=payload.type,
        latitude=payload.latitude,
        longitude=payload.longitude,
        address=payload.address,
        capacity=payload.capacity,
        contact=payload.contact,
        disaster_id=payload.disaster_id,
    )

    await audit_repo.record(
        session,
        entity_type=ENTITY,
        entity_id=resource.id,
        action=AuditAction.CREATE,
        actor_id=actor.id,
        actor_username=actor.username,
        changes={"name": resource.name, "type": str(resource.type)},
        request_id=request_id,
    )

    await session.commit()
    await session.refresh(resource)

    log.info("resource_created", resource_id=str(resource.id), actor=actor.username)
    await events.publish(
        events.EventType.RESOURCE_CREATED,
        {
            "id": str(resource.id),
            "name": resource.name,
            "type": str(resource.type),
            "disaster_id": str(resource.disaster_id) if resource.disaster_id else None,
        },
    )
    return resource


async def get_resource(session: AsyncSession, resource_id: uuid.UUID) -> Resource:
    resource = await resource_repo.get_by_id(session, resource_id)
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} does not exist")
    return resource


def to_response(resource: Resource, *, distance_km: float | None = None) -> ResourceResponse:
    coords = point_to_lat_lng(resource.location)
    if coords is None:
        # The column is NOT NULL, so this means a corrupt row rather than a
        # missing one. Fail loudly instead of emitting a nonsense coordinate.
        raise ValidationError(f"Resource {resource.id} has an unreadable location")

    return ResourceResponse(
        id=resource.id,
        name=resource.name,
        type=resource.type,
        location=GeoPoint(latitude=coords[0], longitude=coords[1]),
        address=resource.address,
        capacity=resource.capacity,
        contact=resource.contact,
        is_active=resource.is_active,
        disaster_id=resource.disaster_id,
        created_at=resource.created_at,
        updated_at=resource.updated_at,
        distance_km=distance_km,
    )
