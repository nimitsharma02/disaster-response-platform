"""Resource model - shelters, hospitals, water points and so on.

Unlike a disaster, ``location`` here is NOT NULL: a resource whose position is
unknown cannot be returned by a proximity search, so it has no reason to exist
in this table.

``disaster_id`` is nullable and that is the interesting part. Most resources are
standing infrastructure that exists independently of any incident (a hospital is
a hospital), while some are stood up in response to one (a temporary shelter).
Modelling it as an optional link lets a nearby-search return both, rather than
forcing every hospital to be duplicated per incident.
"""

from __future__ import annotations

import uuid

from geoalchemy2 import Geography
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import ResourceType


class Resource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resources"

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    type: Mapped[ResourceType] = mapped_column(
        SAEnum(ResourceType, name="resource_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    location: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )

    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Beds, litres/day, people - unit depends on type. Deliberately not modelled
    # more strictly; the brief does not ask for capacity semantics.
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contact: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    # SET NULL rather than CASCADE: if an incident is deleted, a shelter that was
    # opened for it still physically exists. Unlink it, do not destroy it.
    disaster_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("disasters.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        # The index the whole nearby-resources feature depends on.
        Index("ix_resources_location", "location", postgresql_using="gist"),
        # Proximity queries are frequently filtered by type as well ("nearest
        # hospital"), so type gets its own btree for the planner to combine.
        Index("ix_resources_type", "type"),
        Index("ix_resources_disaster_id", "disaster_id"),
    )

    def __repr__(self) -> str:
        return f"<Resource {self.name!r} type={self.type}>"
