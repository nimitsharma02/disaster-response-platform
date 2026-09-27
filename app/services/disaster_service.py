"""Disaster business logic.

This layer owns the rules. Routes handle HTTP, repositories handle SQL, and
everything in between - authorization on a specific record, location resolution,
audit trail, event emission, transaction boundaries - lives here.

The transaction rule is: **one commit per use case, at the end, and events are
published only after it succeeds.** A disaster row, its audit entry, and the
decision to broadcast all belong to the same unit of work. Emitting the event
first would announce a change that could still roll back.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.db.models.disaster import Disaster
from app.db.models.enums import AuditAction, UserRole
from app.realtime import events
from app.repositories import audit_repo, disaster_repo
from app.repositories.geo import point_to_lat_lng
from app.schemas.auth import AuthenticatedUser
from app.schemas.common import GeoPoint, Page, PageMeta
from app.schemas.disaster import (
    DisasterCreate,
    DisasterCreator,
    DisasterListFilters,
    DisasterResponse,
    DisasterUpdate,
    LocationInfo,
)
from app.services import geocoding_service

log = structlog.get_logger(__name__)

ENTITY = "disaster"


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


def assert_can_modify(disaster: Disaster, actor: AuthenticatedUser) -> None:
    """The record-level rule the brief asks to be demonstrated.

    Role alone is not enough here. A contributor is allowed to update *their own*
    incidents, so the check is a function of both the actor and the record - the
    distinction between role-based access control and ownership. Only the service
    layer can enforce it, because only it has loaded the record.

        admin       -> may modify anything
        contributor -> may modify only rows where created_by == their id
        responder    -> never reaches here (blocked by the role gate on the route)
    """
    if actor.role == UserRole.ADMIN:
        return
    if disaster.created_by != actor.id:
        log.warning(
            "authz_denied_not_owner",
            actor=str(actor.id),
            actor_role=str(actor.role),
            disaster=str(disaster.id),
            owner=str(disaster.created_by),
        )
        raise ForbiddenError(
            "You may only modify disasters you created. "
            "Ask an administrator if you need to change this record."
        )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


async def get_disaster(session: AsyncSession, disaster_id: uuid.UUID) -> Disaster:
    disaster = await disaster_repo.get_by_id(session, disaster_id)
    if disaster is None:
        raise NotFoundError(f"Disaster {disaster_id} does not exist")
    return disaster


async def list_disasters(
    session: AsyncSession,
    filters: DisasterListFilters,
    *,
    limit: int,
    offset: int,
) -> Page[DisasterResponse]:
    rows, total = await disaster_repo.list_disasters(
        session,
        tag=filters.tag,
        status=filters.status,
        q=filters.q,
        created_by=filters.created_by,
        lat=filters.lat,
        lng=filters.lng,
        radius_km=filters.radius_km,
        sort=filters.sort,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[to_response(row.disaster, distance_km=row.distance_km) for row in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


async def create_disaster(
    session: AsyncSession,
    payload: DisasterCreate,
    actor: AuthenticatedUser,
    *,
    request_id: str | None = None,
) -> Disaster:
    """Create an incident, resolving its location when coordinates were not given."""
    if payload.latitude is not None and payload.longitude is not None:
        # An explicit coordinate from a human always wins over inference.
        latitude, longitude = payload.latitude, payload.longitude
        location_name = payload.location_name
        source, confidence = "manual", 1.0
    else:
        resolution = await geocoding_service.resolve_from_description(payload.description)
        latitude, longitude = resolution.latitude, resolution.longitude
        location_name = payload.location_name or resolution.location_name
        source, confidence = resolution.source, resolution.confidence

    disaster = await disaster_repo.create(
        session,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        status=payload.status,
        created_by=actor.id,
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        location_source=source,
        location_confidence=confidence,
    )

    await audit_repo.record(
        session,
        entity_type=ENTITY,
        entity_id=disaster.id,
        action=AuditAction.CREATE,
        actor_id=actor.id,
        actor_username=actor.username,
        changes={
            "title": disaster.title,
            "status": str(disaster.status),
            "tags": list(disaster.tags),
            "location_source": source,
        },
        request_id=request_id,
    )

    await session.commit()
    await session.refresh(disaster)

    log.info(
        "disaster_created",
        disaster_id=str(disaster.id),
        actor=actor.username,
        location_source=source,
        resolved=latitude is not None,
    )

    # Only now - the write is durable.
    await events.publish(events.EventType.DISASTER_CREATED, events.disaster_payload(disaster))
    return disaster


async def update_disaster(
    session: AsyncSession,
    disaster_id: uuid.UUID,
    payload: DisasterUpdate,
    actor: AuthenticatedUser,
    *,
    request_id: str | None = None,
) -> Disaster:
    disaster = await get_disaster(session, disaster_id)
    assert_can_modify(disaster, actor)

    # exclude_unset is what makes this a genuine PATCH: a field the client did
    # not send is absent from the dict, so it is left untouched rather than
    # overwritten with a default.
    changes = payload.model_dump(exclude_unset=True, exclude={"reresolve_location"})

    before = _snapshot(disaster)

    # Re-resolve when explicitly asked, or when the description changed and no
    # manual coordinates were supplied with it.
    should_reresolve = payload.reresolve_location or (
        "description" in changes and "latitude" not in changes
    )
    if should_reresolve:
        description = changes.get("description", disaster.description)
        resolution = await geocoding_service.resolve_from_description(description)
        changes["latitude"] = resolution.latitude
        changes["longitude"] = resolution.longitude
        changes["location_source"] = resolution.source
        changes["location_confidence"] = resolution.confidence
        if resolution.location_name and "location_name" not in changes:
            changes["location_name"] = resolution.location_name
    elif "latitude" in changes:
        changes["location_source"] = "manual"
        changes["location_confidence"] = 1.0

    disaster = await disaster_repo.update(session, disaster, changes)

    after = _snapshot(disaster)
    diff = audit_repo.diff(before, after)

    if diff:
        await audit_repo.record(
            session,
            entity_type=ENTITY,
            entity_id=disaster.id,
            action=AuditAction.UPDATE,
            actor_id=actor.id,
            actor_username=actor.username,
            changes=diff,
            request_id=request_id,
        )

    await session.commit()
    await session.refresh(disaster)

    log.info(
        "disaster_updated",
        disaster_id=str(disaster.id),
        actor=actor.username,
        fields=sorted(diff.keys()),
    )

    await events.publish(
        events.EventType.DISASTER_UPDATED,
        {**events.disaster_payload(disaster), "changed_fields": sorted(diff.keys())},
    )
    return disaster


async def delete_disaster(
    session: AsyncSession,
    disaster_id: uuid.UUID,
    actor: AuthenticatedUser,
    *,
    request_id: str | None = None,
) -> None:
    """Delete an incident. Admin-only; the route enforces the role gate.

    Hard delete, with the audit entry as the surviving record. A soft delete
    would be better for a production system (the README says so), but it is the
    kind of scope creep that quietly infects every query with
    ``WHERE deleted_at IS NULL``, so it is left out deliberately rather than
    half-done.
    """
    disaster = await get_disaster(session, disaster_id)
    assert_can_modify(disaster, actor)

    snapshot = _snapshot(disaster)
    payload = events.disaster_payload(disaster)

    await audit_repo.record(
        session,
        entity_type=ENTITY,
        entity_id=disaster.id,
        action=AuditAction.DELETE,
        actor_id=actor.id,
        actor_username=actor.username,
        changes=snapshot,
        request_id=request_id,
    )
    await disaster_repo.delete(session, disaster)
    await session.commit()

    log.info("disaster_deleted", disaster_id=str(disaster_id), actor=actor.username)
    await events.publish(events.EventType.DISASTER_DELETED, payload)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def _snapshot(disaster: Disaster) -> dict[str, Any]:
    """Audit-friendly view of the mutable fields."""
    coords = point_to_lat_lng(disaster.location)
    return {
        "title": disaster.title,
        "description": disaster.description,
        "status": str(disaster.status),
        "tags": list(disaster.tags or []),
        "location_name": disaster.location_name,
        "latitude": coords[0] if coords else None,
        "longitude": coords[1] if coords else None,
        "location_source": disaster.location_source,
    }


def to_response(disaster: Disaster, *, distance_km: float | None = None) -> DisasterResponse:
    """Map an ORM row onto the public response model.

    Done explicitly rather than by ``from_attributes`` alone, because the stored
    geography column is a WKB blob and the API contract is
    ``{latitude, longitude}``. That translation belongs here, at the boundary.
    """
    coords = point_to_lat_lng(disaster.location)

    return DisasterResponse(
        id=disaster.id,
        title=disaster.title,
        description=disaster.description,
        location=LocationInfo(
            name=disaster.location_name,
            point=GeoPoint(latitude=coords[0], longitude=coords[1]) if coords else None,
            source=disaster.location_source,
            confidence=(
                float(disaster.location_confidence)
                if disaster.location_confidence is not None
                else None
            ),
        ),
        tags=list(disaster.tags or []),
        status=disaster.status,
        created_by=disaster.created_by,
        creator=(
            DisasterCreator(
                id=disaster.creator.id,
                username=disaster.creator.username,
                role=str(disaster.creator.role),
            )
            if getattr(disaster, "creator", None) is not None
            else None
        ),
        created_at=disaster.created_at,
        updated_at=disaster.updated_at,
        distance_km=distance_km,
    )
