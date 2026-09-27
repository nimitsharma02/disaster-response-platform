"""Disaster (incident) model, the core aggregate."""

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

    location_name: Mapped[str | None] = mapped_column(String(300), nullable=True)

    location: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )

    location_source: Mapped[str | None] = mapped_column(String(48), nullable=True)
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

    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    creator: Mapped[User] = relationship(back_populates="disasters", lazy="joined")

    __table_args__ = (
        Index("ix_disasters_location", "location", postgresql_using="gist"),
        Index("ix_disasters_tags", "tags", postgresql_using="gin"),
        Index("ix_disasters_status_created_at", "status", text("created_at DESC")),
        Index("ix_disasters_created_by", "created_by"),
        Index("ix_disasters_created_at", text("created_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<Disaster {self.id} {self.title!r} status={self.status}>"
