"""User model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import UserRole

if TYPE_CHECKING:
    from app.db.models.disaster import Disaster


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.CONTRIBUTOR,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    disasters: Mapped[list[Disaster]] = relationship(
        back_populates="creator",
        lazy="noload",
    )

    __table_args__ = (Index("ix_users_role", "role"),)

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"
