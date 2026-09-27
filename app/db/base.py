"""Declarative base and shared column mixins.

The naming convention matters more than it looks. Without it Postgres invents
constraint names, Alembic autogenerate produces churn on every run, and you
cannot write a migration that drops a constraint by name. Setting it once here
makes the schema deterministic.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """UUIDv4 primary keys rather than bigserial.

    Chosen because ids appear in URLs and are emitted over WebSocket: a sequential
    integer leaks how many incidents exist and invites enumeration. The cost is a
    wider index and no natural insertion ordering, which is why ``created_at`` is
    indexed separately for sorting.

    ``gen_random_uuid()`` is a server default (pgcrypto, built into Postgres 13+),
    so rows inserted by SQL or by a seed script get ids without the application.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """Server-generated timestamps.

    ``server_default``/``onupdate`` at the database level, not in Python: the
    clock that matters is the database's, and rows touched by a migration or a
    manual UPDATE stay correct. Always timezone-aware (``timestamptz``) - storing
    naive local times in a system that coordinates across regions is a real bug,
    not a style preference.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
