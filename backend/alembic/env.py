"""Alembic environment.

Wired to:
  - import ``Base.metadata`` from the app's models (autogenerate target)
  - pull the database URL from ``jai.config.Settings`` rather than
    from ``alembic.ini`` (single source of truth)
  - support async migration execution (PostgreSQL + asyncpg)
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import inspect, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Importing the models package populates Base.metadata with every table.
from jai import models  # noqa: F401  (side-effect import)
from jai.config import get_settings
from jai.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override alembic.ini's empty sqlalchemy.url with a migration-owner URL.
# DATABASE_MIGRATION_URL wins for role-specific external deployments;
# DATABASE_URL remains a compatibility override for existing host commands.
# Otherwise Settings safely builds the URL from POSTGRES_MIGRATION_* parts.
_url = get_settings().migration_database_url
# Alembic stores options in ConfigParser, where percent signs are interpolation
# markers. URL.create() correctly percent-encodes reserved password characters,
# so escape them only for ConfigParser; ``get_section`` restores the real URL
# before SQLAlchemy opens the connection.
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live DB)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Apply migrations on a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        # Unreleased M15 snapshots used 0044/0045 and later 0053/0054 before
        # main released SI1/SI2 and M17 under those IDs. Do not conflate schemas.
        if inspect(connection).has_table("agent_token"):
            heads = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
            if heads & {"0044", "0045", "0053", "0054"}:
                raise RuntimeError(
                    "Legacy paused M15 migration history detected (0044/0045 or 0053/0054). "
                    "Those IDs now belong to main's SI1/SI2 or M17; M15 uses 0055/0056. "
                    "Do not stamp or upgrade this database blindly. Preserve a backup "
                    "and reconcile its schema separately, or use a fresh isolated test database."
                )
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine, run migrations, and dispose."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online (live DB) migration."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
