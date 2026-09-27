"""Alembic environment.

The database URL is pulled from application settings at runtime rather than
stored in ``alembic.ini``, so credentials never live in a committed file.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.models import Base  # imports every model, populating Base.metadata

config = context.config
config.set_main_option("sqlalchemy.url", settings.effective_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# GeoAlchemy2 creates these itself when the PostGIS extension is installed.
# Without this filter, autogenerate proposes dropping them on every run.
POSTGIS_MANAGED = {"spatial_ref_sys", "geography_columns", "geometry_columns", "raster_columns"}


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    if type_ == "table" and name in POSTGIS_MANAGED:
        return False
    if type_ == "index" and name and name.startswith("idx_") and reflected:
        # GeoAlchemy2's implicitly-created spatial indexes; ours are named ix_*.
        return False
    return True


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,           # catch VARCHAR(100) -> VARCHAR(200)
        compare_server_default=True,
        render_as_batch=False,       # Postgres has real ALTER TABLE; batch mode is for SQLite
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url else {},
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``)."""
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # a migration run is short-lived; pooling buys nothing
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
