"""§8.3 liveness: the engine lock is visible in pg_locks for THIS database only."""
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock, advisory_lock_held
from src.services.simulation_control import engine_alive

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_holding_the_lock_makes_the_engine_alive_and_closing_releases_it(engine, pg_url):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lock = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await lock.acquire() is True
    try:
        async with factory() as db:
            assert await engine_alive(db) is True
        second = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
        assert await second.acquire() is False
        await second.release()
        await lock.check()
    finally:
        await lock.release()
    async with factory() as db:
        assert await engine_alive(db) is False


@pytest.mark.asyncio
async def test_killing_the_holder_connection_releases_the_lock(engine, pg_url):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lock = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await lock.acquire()
    pid = await lock.backend_pid()
    async with engine.connect() as conn:
        # Wait up to 5 s for the backend to exit so the assertion is not racy.
        await conn.execute(text("SELECT pg_terminate_backend(:p, 5000)"), {"p": pid})
    alive = True
    for _ in range(50):
        async with factory() as db:
            alive = await engine_alive(db)
        if not alive:
            break
        await asyncio.sleep(0.1)
    assert alive is False
    with pytest.raises(DBAPIError):  # the terminated connection
        await lock.check()
    await lock.release()


@pytest.mark.asyncio
async def test_a_negative_key_is_matched(engine, pg_url):
    negative = -(2**62) - 12345
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lock = SessionAdvisoryLock(pg_url, negative)
    assert await lock.acquire()
    try:
        async with factory() as db:
            assert await advisory_lock_held(db, negative) is True
            assert await advisory_lock_held(db, negative + 1) is False
    finally:
        await lock.release()


@pytest.mark.asyncio
async def test_a_lock_in_another_database_is_ignored(engine, pg_url):
    name = f"copi_lockcheck_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            try:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
            except DBAPIError as exc:
                pytest.skip(f"cannot create a scratch database here: {exc}")
        other_url = pg_url.rsplit("/", 1)[0] + f"/{name}"
        lock = SessionAdvisoryLock(other_url, ENGINE_LOCK_KEY)
        assert await lock.acquire()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                assert await engine_alive(db) is False
        finally:
            await lock.release()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}"'))
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_entity_keys_are_deterministic_and_signed(engine):
    from src.services.advisory_locks import entity_key_sql

    uid = str(uuid.uuid4())
    async with engine.connect() as conn:
        sql = text(f"SELECT {entity_key_sql('corpus')}")
        first = (await conn.execute(sql, {"id": uid})).scalar_one()
        second = (await conn.execute(sql, {"id": uid})).scalar_one()
        other = (await conn.execute(text(f"SELECT {entity_key_sql('provision')}"), {"id": uid})).scalar_one()
    assert first == second and first != other
    assert -(2**63) <= first < 2**63
