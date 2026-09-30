"""§8.3: a second engine raises before any write and before archiving memory."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.engine.control import EngineAlreadyRunning
from src.models import SimulationRun
from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_a_second_engine_raises_before_any_side_effect(engine, pg_url, monkeypatch):
    from src.agent import main as agent_main
    from src.config import get_settings

    monkeypatch.setenv("DATABASE_URL", pg_url)
    get_settings.cache_clear()

    def _archive(*a, **kw):
        raise AssertionError("memory was archived before the lock was checked")

    monkeypatch.setattr("src.agent.working_memory_reset.archive_working_memory", _archive)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        before = await db.scalar(select(func.count(SimulationRun.id)))
    holder = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await holder.acquire()
    try:
        with pytest.raises(EngineAlreadyRunning):
            await agent_main._run_simulation(0, 0, False, False, True)
    finally:
        await holder.release()
        get_settings.cache_clear()
    async with factory() as db:
        assert await db.scalar(select(func.count(SimulationRun.id))) == before
