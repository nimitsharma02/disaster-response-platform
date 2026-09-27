"""Disaster data access.

The repository layer owns SQL and nothing else. It does not know what HTTP is,
does not enforce permissions, and does not emit events - that is the service
layer's job. Keeping it that narrow is what lets the business rules be tested
without a web server and the queries be read without wading through policy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.disaster import Disaster
from app.db.models.enums import DisasterStatus
from app.repositories.geo import distance_km, geography_point, within_radius


@dataclass(slots=True)
class DisasterRow:
    """A disaster plus the distance from a query point, when there was one."""

    disaster: Disaster
    distance_km: float | None = None


def _apply_filters(
    stmt: Select,
    *,
    tag: str | None,
    status: DisasterStatus | None,
    q: str | None,
    created_by: uuid.UUID | None,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
) -> Select:
    """Compose the WHERE clause.

    Shared by the page query and the count query, so the total can never
    disagree with the rows - a classic pagination bug when the two are written
    out separately.
    """
    if tag:
        # Array containment. Uses the GIN index; `tag = ANY(tags)` would not.
        stmt = stmt.where(Disaster.tags.contains([tag]))

    if status is not None:
        stmt = stmt.where(Disaster.status == status)

    if q:
        # Leading-wildcard ILIKE, backed by the pg_trgm GIN index created in the
        # initial migration. Adequate for an operator search box; a real
        # full-text requirement would want tsvector + ts_rank instead, which is
        # noted as a trade-off in the README.
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Disaster.title.ilike(pattern), Disaster.description.ilike(pattern)))

    if created_by is not None:
        stmt = stmt.where(Disaster.created_by == created_by)

    if lat is not None and lng is not None and radius_km:
        # Rows with an unresolved location cannot satisfy a proximity filter.
        # ST_DWithin on NULL yields NULL (not false), so exclude them explicitly.
        stmt = stmt.where(
            Disaster.location.isnot(None),
            within_radius(Disaster.location, lat, lng, radius_km),
        )

    return stmt


_SORTS = {
    "created_at": asc(Disaster.created_at),
    "-created_at": desc(Disaster.created_at),
    "title": asc(Disaster.title),
    "-title": desc(Disaster.title),
    "updated_at": asc(Disaster.updated_at),
    "-updated_at": desc(Disaster.updated_at),
}


async def list_disasters(
    session: AsyncSession,
    *,
    tag: str | None = None,
    status: DisasterStatus | None = None,
    q: str | None = None,
    created_by: uuid.UUID | None = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    sort: str = "-created_at",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[DisasterRow], int]:
    """Return one page of disasters plus the unpaginated total."""
    has_point = lat is not None and lng is not None

    # Only select the distance column when there is a point to measure from;
    # computing it otherwise would be wasted work on every row.
    if has_point:
        distance = distance_km(Disaster.location, lat, lng).label("distance_km")
        stmt = select(Disaster, distance)
    else:
        stmt = select(Disaster)

    stmt = _apply_filters(
        stmt, tag=tag, status=status, q=q, created_by=created_by,
        lat=lat, lng=lng, radius_km=radius_km,
    )

    if sort == "distance" and has_point:
        # NULLS LAST so unlocated incidents sort to the end rather than the front
        # (in Postgres, NULL sorts last on ASC by default, but being explicit
        # here survives a future switch to DESC).
        stmt = stmt.order_by(
            distance_km(Disaster.location, lat, lng).asc().nullslast()
        )
    else:
        stmt = stmt.order_by(_SORTS.get(sort, desc(Disaster.created_at)))

    # Deterministic tie-break. Without it, two rows sharing a created_at can swap
    # between pages and an item is shown twice or skipped entirely.
    stmt = stmt.order_by(desc(Disaster.id))

    page = await session.execute(stmt.limit(limit).offset(offset))

    # Count over the same predicates. select_from is required because the
    # distance column would otherwise confuse the implicit FROM inference.
    count_stmt = _apply_filters(
        select(func.count(Disaster.id)).select_from(Disaster),
        tag=tag, status=status, q=q, created_by=created_by,
        lat=lat, lng=lng, radius_km=radius_km,
    )
    total = int((await session.execute(count_stmt)).scalar_one())

    if has_point:
        rows = [DisasterRow(disaster=d, distance_km=_round(dist)) for d, dist in page.all()]
    else:
        rows = [DisasterRow(disaster=d) for d in page.scalars().all()]

    return rows, total


def _round(value: float | None) -> float | None:
    # Three decimals is metre precision. More digits imply an accuracy the
    # underlying geocoding does not have.
    return round(float(value), 3) if value is not None else None


async def get_by_id(session: AsyncSession, disaster_id: uuid.UUID) -> Disaster | None:
    result = await session.execute(select(Disaster).where(Disaster.id == disaster_id))
    return result.unique().scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    title: str,
    description: str,
    tags: list[str],
    status: DisasterStatus,
    created_by: uuid.UUID,
    location_name: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_source: str | None = None,
    location_confidence: float | None = None,
) -> Disaster:
    disaster = Disaster(
        title=title,
        description=description,
        tags=tags,
        status=status,
        created_by=created_by,
        location_name=location_name,
        location=(
            geography_point(latitude, longitude)
            if latitude is not None and longitude is not None
            else None
        ),
        location_source=location_source,
        location_confidence=location_confidence,
    )
    session.add(disaster)
    await session.flush()
    # The location was written as a SQL expression, so the in-memory attribute
    # still holds that expression rather than a value. Refresh to read back what
    # the database actually stored.
    await session.refresh(disaster)
    return disaster


async def update(
    session: AsyncSession,
    disaster: Disaster,
    changes: dict[str, Any],
) -> Disaster:
    """Apply a partial update.

    ``changes`` must already be reduced to the fields the caller actually set -
    the service layer does that with ``model_dump(exclude_unset=True)``, which is
    what keeps "not provided" distinct from "set to null".
    """
    coords_changed = "latitude" in changes or "longitude" in changes
    latitude = changes.pop("latitude", None)
    longitude = changes.pop("longitude", None)

    for field, value in changes.items():
        setattr(disaster, field, value)

    if coords_changed:
        disaster.location = (
            geography_point(latitude, longitude)
            if latitude is not None and longitude is not None
            else None
        )

    await session.flush()
    await session.refresh(disaster)
    return disaster


async def delete(session: AsyncSession, disaster: Disaster) -> None:
    await session.delete(disaster)
    await session.flush()


async def list_active_ids(session: AsyncSession, *, limit: int = 100) -> list[uuid.UUID]:
    """Ids of incidents still being responded to.

    Used by the background worker to decide which report caches are worth
    pre-warming: there is no value in refreshing reports for a resolved incident.
    """
    stmt = (
        select(Disaster.id)
        .where(
            Disaster.status.in_(
                [DisasterStatus.REPORTED, DisasterStatus.VERIFIED, DisasterStatus.IN_PROGRESS]
            )
        )
        .order_by(desc(Disaster.updated_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
