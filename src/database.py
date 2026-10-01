"""SQLAlchemy async engine and session factory."""

from collections.abc import AsyncGenerator
from typing import Any, Literal

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from src.config import get_settings


class Base(DeclarativeBase):
    pass


EngineRole = Literal[
    "web", "worker", "agent", "supervisor", "agent_roster", "cli", "heartbeat", "advisory_lock",
    "provision_lock",
]


def engine_kwargs(role: EngineRole) -> dict[str, Any]:
    """Each call site's create_async_engine arguments, unchanged by the consolidation."""
    if role == "web":
        # A DB restart otherwise leaves stale pooled connections that each
        # 500 one request; pre-ping revalidates on checkout (issue #25 P3).
        # The agent process's own engine has carried pool_pre_ping=True since it
        # sized its own pool — that half is the web-tier transplant. It does NOT
        # set pool_recycle, so pool_recycle=1800 is a web-only addition:
        # retiring long-lived connections before infra does.
        return {"echo": False, "pool_size": 5, "max_overflow": 10,
                "pool_pre_ping": True, "pool_recycle": 1800}
    if role == "worker":
        return {"echo": False}
    if role == "agent":
        s = get_settings()
        return {"pool_size": s.db_pool_size, "max_overflow": s.db_max_overflow, "pool_pre_ping": True}
    if role == "supervisor":
        return {"pool_pre_ping": True}
    if role == "heartbeat":
        # The engine heartbeat's own one-connection pool, so a saturated main
        # pool cannot starve the liveness tick.
        return {"pool_size": 1, "max_overflow": 0, "pool_pre_ping": True}
    if role == "advisory_lock":
        # SessionAdvisoryLock: the lock lives exactly as long as one dedicated
        # connection, so no pooling and no implicit transaction.
        return {"poolclass": NullPool, "isolation_level": "AUTOCOMMIT"}
    if role == "provision_lock":
        # The per-agent Slack provisioning lock (RA-15): a transaction-level advisory
        # lock held across the Slack call, so a dedicated unpooled connection (never
        # the web pool) that keeps its transaction (not AUTOCOMMIT).
        return {"poolclass": NullPool}
    if role in ("agent_roster", "cli"):
        return {}
    raise ValueError(f"unknown engine role {role!r}")


def make_engine(role: EngineRole, database_url: str | None = None) -> AsyncEngine:
    """Build the async engine for ``role``; ``database_url`` overrides the setting."""
    return create_async_engine(database_url or get_settings().database_url, **engine_kwargs(role))


def _get_engine():
    return make_engine("web")


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
