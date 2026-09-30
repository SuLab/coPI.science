"""Engine start refuses a schema without migration 0055's
uq_opportunity_assessments_run_thread: on 0054 every threaded verdict write
would fail. A failure-path refusal only; a migrated database starts as before."""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.engine.control import EngineConfigError, require_verdict_schema
from src.models import SimulationProcessStatus, SimulationRun
from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock

pytestmark = pytest.mark.integration

_CONSTRAINT = "uq_opportunity_assessments_run_thread"


@pytest.mark.asyncio
async def test_the_check_passes_at_head_and_refuses_without_the_constraint(engine):
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            db = AsyncSession(bind=conn)
            await require_verdict_schema(db)
            await conn.execute(text(f"ALTER TABLE opportunity_assessments DROP CONSTRAINT {_CONSTRAINT}"))
            with pytest.raises(EngineConfigError, match="migration 0055"):
                await require_verdict_schema(db)
        finally:
            await trans.rollback()
    async with engine.connect() as conn:
        assert await conn.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = :n)"
        ), {"n": _CONSTRAINT}), "the rollback restored the constraint"


@pytest.mark.asyncio
async def test_engine_start_refuses_before_any_side_effect(engine, pg_url, monkeypatch):
    from src.agent import main as agent_main
    from src.config import get_settings

    monkeypatch.setenv("DATABASE_URL", pg_url)
    get_settings.cache_clear()
    # The real query, asked about a constraint this database does not have.
    monkeypatch.setattr("src.agent.engine.control.VERDICT_SCHEMA_CONSTRAINT", "uq_not_in_this_schema")

    def _archive(*a, **kw):
        raise AssertionError("memory was archived before the schema was checked")

    monkeypatch.setattr("src.agent.working_memory_reset.archive_working_memory", _archive)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        before = await db.scalar(select(func.count(SimulationRun.id)))
    try:
        with pytest.raises(EngineConfigError, match="migration 0055"):
            await agent_main._run_simulation(0, 0, False, False, True)
        async with factory() as db:
            assert await db.scalar(select(func.count(SimulationRun.id))) == before
        probe = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
        try:
            assert await probe.acquire(), "the refused start released the engine lock"
        finally:
            await probe.release()
    finally:
        get_settings.cache_clear()
        async with factory() as db:
            status = await db.get(SimulationProcessStatus, 1)
            if status is not None:
                await db.delete(status)
                await db.commit()
