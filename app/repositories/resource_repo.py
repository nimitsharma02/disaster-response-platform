"""Resource data access, including the nearby-resources query.

``find_nearby`` is the query the whole geospatial requirement comes down to. It
compiles to roughly::

    SELECT r.*,
           ST_Distance(r.location, $point) / 1000.0 AS distance_km
    FROM resources r
    WHERE r.is_active
      AND ST_DWithin(r.location, $point, $radius_km * 1000)   -- GiST index
      AND (r.type = $type)                                    -- optional
    ORDER BY ST_Distance(r.location, $point)
    LIMIT $limit;

where ``$point`` is ``ST_SetSRID(ST_MakePoint($lng, $lat), 4326)::geography``.

See ``app/repositories/geo.py`` for why it is ``ST_DWithin`` and not
``ST_Distance(...) < r`` - that choice is the difference between an index scan
and a sequential one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import ResourceType
from app.db.models.resource import Resource
from app.repositories.geo import distance_km, geography_point, within_radius


@dataclass(slots=True)
class ResourceRow:
    resource: Resource
    distance_km: float | None = None


async def find_nearby(
    session: AsyncSession,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    resource_type: ResourceType | None = None,
    disaster_id: uuid.UUID | None = None,
    include_unlinked: bool = True,
    limit: int = 50,
) -> list[ResourceRow]:
    """Resources within ``radius_km`` of a point, nearest first.

    ``include_unlinked`` defaults to True because most useful resources are
    standing infrastructure with no link to any particular incident. A hospital
    two kilometres away is relevant whether or not someone remembered to
    associate it with this disaster.
    """
    distance = distance_km(Resource.location, lat, lng).label("distance_km")

    stmt = (
        select(Resource, distance)
        .where(
            Resource.is_active.is_(True),
            within_radius(Resource.location, lat, lng, radius_km),
        )
        # Sorting by the raw metre distance avoids recomputing the /1000 division
        # per comparison; the ordering is identical either way.
        .order_by(func.ST_Distance(Resource.location, geography_point(lat, lng)).asc())
        .limit(limit)
    )

    if resource_type is not None:
        stmt = stmt.where(Resource.type == resource_type)

    if disaster_id is not None and not include_unlinked:
        stmt = stmt.where(Resource.disaster_id == disaster_id)

    result = await session.execute(stmt)
    return [
        ResourceRow(resource=r, distance_km=round(float(d), 3) if d is not None else None)
        for r, d in result.all()
    ]


async def get_by_id(session: AsyncSession, resource_id: uuid.UUID) -> Resource | None:
    return await session.get(Resource, resource_id)


async def list_resources(
    session: AsyncSession,
    *,
    resource_type: ResourceType | None = None,
    disaster_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Resource], int]:
    stmt = select(Resource).where(Resource.is_active.is_(True))
    count_stmt = select(func.count(Resource.id)).where(Resource.is_active.is_(True))

    if resource_type is not None:
        stmt = stmt.where(Resource.type == resource_type)
        count_stmt = count_stmt.where(Resource.type == resource_type)
    if disaster_id is not None:
        stmt = stmt.where(Resource.disaster_id == disaster_id)
        count_stmt = count_stmt.where(Resource.disaster_id == disaster_id)

    stmt = stmt.order_by(Resource.name).limit(limit).offset(offset)

    rows = list((await session.execute(stmt)).scalars().all())
    total = int((await session.execute(count_stmt)).scalar_one())
    return rows, total


async def create(
    session: AsyncSession,
    *,
    name: str,
    resource_type: ResourceType,
    latitude: float,
    longitude: float,
    address: str | None = None,
    capacity: int | None = None,
    contact: str | None = None,
    disaster_id: uuid.UUID | None = None,
) -> Resource:
    resource = Resource(
        name=name,
        type=resource_type,
        location=geography_point(latitude, longitude),
        address=address,
        capacity=capacity,
        contact=contact,
        disaster_id=disaster_id,
    )
    session.add(resource)
    await session.flush()
    await session.refresh(resource)
    return resource


async def update(session: AsyncSession, resource: Resource, changes: dict[str, Any]) -> Resource:
    coords_changed = "latitude" in changes or "longitude" in changes
    latitude = changes.pop("latitude", None)
    longitude = changes.pop("longitude", None)

    for field, value in changes.items():
        setattr(resource, field, value)

    if coords_changed and latitude is not None and longitude is not None:
        resource.location = geography_point(latitude, longitude)

    await session.flush()
    await session.refresh(resource)
    return resource


async def count_all(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(Resource.id)))).scalar_one())
