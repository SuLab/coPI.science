"""SQLAlchemy async engine and session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import get_settings


class Base(DeclarativeBase):
    pass


def _get_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        # A DB restart otherwise leaves stale pooled connections that each
        # 500 one request; pre-ping revalidates on checkout (issue #25 P3).
        # The agent process's own engine (src/agent/main.py) has carried
        # pool_pre_ping=True since it sized its own pool — that half is the
        # web-tier transplant. It does NOT set pool_recycle (verified
        # 2026-08-21: no such argument anywhere in agent/main.py), so
        # pool_recycle=1800 below is a new addition on its own merits —
        # retiring long-lived connections before infra does — not a
        # transplant of something the agent process already does.
        pool_pre_ping=True,
        pool_recycle=1800,
    )


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
