import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import the app's models so `Base.metadata` reflects the real current schema for
# autogenerate. This repo has no other models module — everything lives in
# backend/models.py (see backend/database.py for the engine/session setup this
# mirrors).
from backend.models import Base  # noqa: E402

target_metadata = Base.metadata

# Same env var + default as backend/database.py — don't hardcode a different DB URL
# scheme/convention here. The [alembic] sqlalchemy.url in alembic.ini is just a
# placeholder; this always wins when set (and DATABASE_URL is always set in prod).
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:////data/vrising.db")
config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite can't ALTER a constraint/column in place (no native DROP CONSTRAINT,
        # very limited ALTER COLUMN) — batch mode makes Alembic emit the copy-to-new-
        # table / copy-data / drop-old-table / rename dance instead, which is the only
        # way SQLite supports these changes at all. Every migration in this repo
        # targets SQLite (see DATABASE_URL's default), so this is on unconditionally
        # rather than dialect-sniffed. This replaces the hand-rolled RENAME/CREATE/
        # COPY/DROP that used to live in backend/main.py's lifespan() for the
        # poll_votes UNIQUE-constraint rebuild specifically.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, via the async engine + run_sync recipe
    (SQLAlchemy 2.0 async engines can't run migrations directly — Alembic's
    migration context is sync, so we bridge with connection.run_sync())."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
