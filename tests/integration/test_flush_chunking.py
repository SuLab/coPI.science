"""The agent_messages flush is chunked and serialized.

The upsert binds 17 parameters per row, and asyncpg refuses a statement past
32,767 binds: 1,928 queued rows failed with InterfaceError, which is not in
_ROW_LEVEL_DB_ERRORS, so the whole batch re-queued and failed forever.
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import PERSIST_UPSERT_CHUNK_ROWS, SimulationEngine
from src.models import AgentMessage, SimulationRun

pytestmark = pytest.mark.integration


async def _new_run(factory):
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
        return run.id


async def _delete_run(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


def _entry(i):
    ts = f"{1_600_001_000 + i}.000000"
    return LogEntry(
        ts=ts, channel="general", sender_agent_id="wang", sender_name="WangBot",
        content=f"message {i}", posted_at=float(ts), is_bot=True,
    )


def _engine(factory, run_id):
    return SimulationEngine(
        agents=[Agent("wang", "WangBot", "Wang")], slack_clients={},
        session_factory=factory, simulation_run_id=run_id,
    )


async def _stored_ts(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AgentMessage.message_ts).where(AgentMessage.simulation_run_id == run_id)
        )).scalars().all()


def test_a_chunk_stays_far_below_the_bind_limit():
    assert PERSIST_UPSERT_CHUNK_ROWS * 17 < 32_767


async def test_2000_queued_entries_land_in_one_flush(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        eng = _engine(factory, run_id)
        eng._pending_persist = [_entry(i) for i in range(2000)]
        await eng._flush_persisted()
        assert eng._pending_persist == []
        stored = await _stored_ts(factory, run_id)
        assert len(stored) == 2000 and len(set(stored)) == 2000
    finally:
        await _delete_run(factory, run_id)


class _SessionProbe:
    """Wraps a session factory and records the peak number of open sessions."""

    def __init__(self, real):
        self._real = real
        self.open = 0
        self.peak = 0

    def __call__(self):
        probe = self
        cm = self._real()

        class _Session:
            async def __aenter__(self):
                probe.open += 1
                probe.peak = max(probe.peak, probe.open)
                return await cm.__aenter__()

            async def __aexit__(self, *exc_info):
                try:
                    return await cm.__aexit__(*exc_info)
                finally:
                    probe.open -= 1

        return _Session()


async def test_concurrent_flushes_serialize_and_neither_drop_nor_duplicate(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    probe = _SessionProbe(factory)
    try:
        eng = _engine(probe, run_id)
        eng._pending_persist = [_entry(i) for i in range(1200)]
        first = asyncio.create_task(eng._flush_persisted())
        await asyncio.sleep(0)  # `first` has taken the buffer and is awaiting the DB
        eng._pending_persist.extend(_entry(i) for i in range(1200, 1500))
        second = asyncio.create_task(eng._flush_persisted())
        await asyncio.gather(first, second)

        assert eng._pending_persist == []
        assert probe.peak == 1, "two flushes held database sessions at the same time"
        stored = await _stored_ts(factory, run_id)
        assert len(stored) == 1500 and len(set(stored)) == 1500
    finally:
        await _delete_run(factory, run_id)
