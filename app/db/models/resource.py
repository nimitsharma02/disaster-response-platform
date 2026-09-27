"""Resource model, shelters, hospitals, water points and so on."""

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
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contact: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    disaster_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("disasters.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_resources_location", "location", postgresql_using="gist"),
        Index("ix_resources_type", "type"),
        Index("ix_resources_disaster_id", "disaster_id"),
    )

    def __repr__(self) -> str:
        return f"<Resource {self.name!r} type={self.type}>"
