"""User data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import UserRole
from app.db.models.user import User


async def get_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(
        select(User).where(User.username == username.strip().lower())
    )
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def list_all(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).order_by(User.username))
    return list(result.scalars().all())


async def create(
    session: AsyncSession,
    *,
    username: str,
    password_hash: str,
    role: UserRole,
    display_name: str | None = None,
) -> User:
    user = User(
        username=username.strip().lower(),
        password_hash=password_hash,
        role=role,
        display_name=display_name,
    )
    session.add(user)
    await session.flush()  # assigns the server-generated id without committing
    return user
