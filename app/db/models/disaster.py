"""Disaster (incident) model - the core aggregate.

Three decisions worth defending:

**``location`` is ``geography(Point, 4326)`` and nullable.** Geography rather than
geometry because distances come back in metres on a spheroid with no projection
to choose, which is what a "resources within 5 km" query actually means. Nullable
because location resolution is a best-effort inference from free text: when a
description names no recognisable place, recording the incident with an unknown
location is strictly better than rejecting it. A coordination tool must accept a
report made under pressure.

**``tags`` is ``text[]`` with a GIN index, not a join table.** Tags here are a
small, closed, read-mostly vocabulary that is only ever queried by containment
(``tags @> ARRAY['flood']``). A join table would add two joins to every list
query to buy referential integrity nobody needs. If tags ever gained attributes
of their own - descriptions, colours, ownership - that calculus flips.

**Resolution provenance is stored.** ``location_source`` and
``location_confidence`` record *how* we arrived at the coordinates. An operator
routing rescue crews needs to know the difference between "an LLM guessed this
from a tweet" and "a human entered it", and an API that hides that distinction is
lying by omission.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from geoalchemy2 import Geography
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import DisasterStatus

if TYPE_CHECKING:
    from app.db.models.user import User


class Disaster(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "disasters"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Human-readable place as extracted from the description ("Manhattan, NYC").
    location_name: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Resolved coordinates. SRID 4326 = WGS 84, the lat/lng system GPS and every
    # mapping provider speak. spatial_index=False because the GiST index is
    # declared explicitly in __table_args__ so it carries our naming convention.
    location: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )

    # 'llm+nominatim' | 'rules+nominatim' | 'rules+gazetteer' | 'manual' | 'unresolved'
    location_source: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # 0.00-1.00. Numeric, not float: exact, comparable, and it round-trips in JSON.
    location_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)

    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )

    status: Mapped[DisasterStatus] = mapped_column(
        SAEnum(
            DisasterStatus,
            name="disaster_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DisasterStatus.REPORTED,
        server_default=DisasterStatus.REPORTED.value,
    )

    # RESTRICT, not CASCADE: deleting a user must not silently erase the incident
    # history they filed. Deactivate the user instead.
    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    creator: Mapped[User] = relationship(back_populates="disasters", lazy="joined")

    __table_args__ = (
        # GiST is the index type that makes ST_DWithin fast: it stores bounding
        # boxes in a tree so the planner can discard most rows before running the
        # exact (expensive) distance calculation on the survivors.
        Index("ix_disasters_location", "location", postgresql_using="gist"),
        # GIN inverted index: one entry per distinct tag pointing at the rows that
        # carry it, which is what makes `tags @> ARRAY['flood']` an index lookup
        # rather than a scan over every row's array.
        Index("ix_disasters_tags", "tags", postgresql_using="gin"),
        # Composite, in this order, because the common list query filters on
        # status and then sorts by recency - Postgres can satisfy both from one
        # index without a separate sort step.
        Index("ix_disasters_status_created_at", "status", text("created_at DESC")),
        Index("ix_disasters_created_by", "created_by"),
        Index("ix_disasters_created_at", text("created_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<Disaster {self.id} {self.title!r} status={self.status}>"
