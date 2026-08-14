"""Alembic environment for async SQLAlchemy."""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import models to register them with Base.metadata
import src.models  # noqa: F401
from src.database import Base

# this is the Alembic Config object
config = context.config

# Override sqlalchemy.url from env if DATABASE_URL is set
db_url = os.environ.get("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


#: How long a migration will WAIT for a lock before giving up, in milliseconds.
#: 0 disables the bound (Postgres' own default, and what this file did before).
#:
#: Why this exists. `context.configure()` below is deliberately NOT passed
#: `transaction_per_migration`, so the entire upgrade chain runs in ONE
#: transaction. That is good — a killed migration cannot leave a half-applied
#: schema, verified by terminating the backend mid-chain. But it means every lock
#: the chain takes is held until the final commit, and migration 0019 takes
#: ACCESS EXCLUSIVE on `agent_messages` to add three indexes and a unique
#: constraint (which brings a fourth index of its own).
#:
#: With no lock_timeout, `alembic upgrade` parked behind a single open
#: `BEGIN; SELECT …` waits forever — and because a pending ACCESS EXCLUSIVE
#: request queues ahead of new readers, every subsequent query on that table
#: blocks behind it. One forgotten transaction plus a migration is a total stall
#: on the hot table, with no timeout to end it. Failing fast and retrying in a
#: quieter moment is strictly better than an unbounded outage: the transaction
#: rolls back cleanly, so a timeout costs nothing but the attempt.
#:
#: This bounds only the WAIT for a lock. It is not `statement_timeout`, which
#: would cancel a legitimately long index build partway through.
LOCK_TIMEOUT_MS = os.environ.get("ALEMBIC_LOCK_TIMEOUT_MS", "10000")


#: NOTE ON HOW THIS IS APPLIED. It is set as an asyncpg *connect* setting, not by
#: executing `SET lock_timeout = …` on the connection inside do_run_migrations().
#: The obvious version of that is quietly catastrophic: `connection.exec_driver_sql`
#: before `context.begin_transaction()` opens its own transaction, alembic's
#: transaction then nests inside it, and the outer `async with connect()` exits
#: without committing — so every migration LOGS "Running upgrade" and the whole
#: chain SILENTLY ROLLS BACK, leaving no `alembic_version` table at all. Observed
#: while writing this: 18 migrations "applied", nothing persisted. Counting the log
#: lines is not a verification; always re-read `alembic_version` afterwards.
def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine and associate a connection with the context."""
    cfg = config.get_section(config.config_ini_section, {})
    kwargs: dict = {"prefix": "sqlalchemy.", "poolclass": pool.NullPool}
    if LOCK_TIMEOUT_MS and LOCK_TIMEOUT_MS != "0":
        # asyncpg takes libpq-style GUCs via server_settings, applied at connect
        # time — outside any transaction, so it cannot disturb alembic's.
        kwargs["connect_args"] = {
            "server_settings": {"lock_timeout": str(int(LOCK_TIMEOUT_MS))}
        }
    connectable = async_engine_from_config(cfg, **kwargs)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
