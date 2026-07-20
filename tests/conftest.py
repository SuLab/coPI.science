"""Shared test harness: ephemeral migrated Postgres + txn-rollback session + ASGI client.

Integration/characterization/contract tests require a real Postgres (the bugs worth
pinning only reproduce on PG, not SQLite). We spin an ephemeral container per session,
migrate it with the real alembic chain (NOT create_all — that omits migration-only
indexes/constraints), and run each test inside a transaction that is rolled back, so
tests never see each other's writes and the dev DB is untouched.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def _pg_container():
    with PostgresContainer("postgres:15", dbname="copi_test") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(_pg_container):
    """asyncpg DSN for the ephemeral container (creds come from the container, not hardcoded)."""
    return (
        f"postgresql+asyncpg://{_pg_container.username}:{_pg_container.password}"
        f"@{_pg_container.get_container_host_ip()}:{_pg_container.get_exposed_port(5432)}"
        f"/{_pg_container.dbname}"
    )


@pytest.fixture(scope="session")
def _migrated(pg_url):
    """Run the real alembic chain against the container (schema fidelity vs create_all)."""
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    env = {**os.environ, "DATABASE_URL": pg_url}
    r = subprocess.run(
        [alembic, "upgrade", "head"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"alembic upgrade head failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    return pg_url


@pytest.fixture(scope="session")
def engine(_migrated):
    """Session-scoped AsyncEngine. NullPool so each connect() opens a fresh asyncpg
    connection in the current test's event loop — pytest-asyncio uses a per-test loop,
    and a pooled asyncpg connection bound to test A's loop errors ("another operation
    is in progress") when reused in test B's loop."""
    eng = create_async_engine(_migrated, future=True, poolclass=NullPool)
    yield eng
    asyncio.run(eng.dispose())


@pytest_asyncio.fixture
async def db_session(engine):
    """Function-scoped session whose writes (and route code's commits) roll back after the test."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",  # session.commit() -> savepoint release
        )
        try:
            yield session
        finally:
            await session.close()
            if trans.is_active:
                await trans.rollback()


@pytest_asyncio.fixture
async def client(db_session):
    """ASGI TestClient over create_app() with get_db routed to the rolled-back session."""
    from src.database import get_db
    from src.main import create_app

    app = create_app()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def _text():
    return text
