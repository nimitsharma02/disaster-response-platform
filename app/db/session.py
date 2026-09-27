"""Async engine, session factory and the FastAPI session dependency.

One engine per process (it owns the connection pool), one session per request.
A session is a unit of work: it must not be shared between requests, because it
holds identity-map state and an open transaction.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

log = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.effective_database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # Recycle below typical proxy/firewall idle timeouts so a checked-out
            # connection is never already dead when a request grabs it.
            pool_recycle=1800,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            # Keep attributes loaded after commit so a response model can still
            # read them; otherwise every field access triggers a lazy refresh
            # against a session we are about to close.
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request, always closed.

    Commit is the *service layer's* responsibility, not the dependency's - a
    request that raises after a partial write must not have it silently
    committed. This only guarantees rollback-on-error and close-on-exit.
    """
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


async def check_database() -> bool:
    """Readiness probe: can we actually round-trip a query?"""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("database_healthcheck_failed", error=str(exc))
        return False
