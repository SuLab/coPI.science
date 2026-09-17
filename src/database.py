"""SQLAlchemy async engine and session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str, **overrides):
    """Build an async engine with pool defaults tuned for long-lived processes.

    pool_pre_ping issues a lightweight probe before handing out a pooled
    connection, so a connection left stale by a DB restart/failover is
    discarded and replaced instead of surfacing as a 500 on first use.
    pool_recycle forces connections older than 1800s to be replaced, ahead of
    typical infra idle-connection kill windows. pool_timeout bounds how long
    a checkout waits when the pool is exhausted. Explicit ``overrides`` (e.g.
    pool_size/max_overflow) win over these defaults.

    These defaults assume a QueuePool-family pool (SQLAlchemy's async default);
    passing ``poolclass=NullPool`` alongside them raises ``TypeError`` (NullPool
    accepts none of pool_size/pool_timeout/pool_recycle) — no current caller does
    this, so it is a documentation note, not a guard.
    """
    kwargs = {
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_timeout": 30,
    }
    kwargs.update(overrides)
    return create_async_engine(url, **kwargs)


def _get_engine():
    settings = get_settings()
    return make_engine(settings.database_url, pool_size=5, max_overflow=10)


_engine = None
_async_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _get_engine()
    return _engine


def get_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database sessions."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
