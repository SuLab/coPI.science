"""A post is persisted before `_post_message` returns.

Closes the window between a Slack post and the next tick's flush: what remains is
the instant between Slack accepting the message and the DB write. A flush failure
keeps the entry buffered (the existing re-queue) and the post still counts as
posted — it is on Slack.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.models import AgentMessage, SimulationRun
from tests.fakes import FakeSlackClient

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


def _engine(monkeypatch, tmp_path, factory, run_id):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    agents = [Agent("wang", "WangBot", "Wang"), Agent("gordy", "GordyBot", "Gordy")]
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in agents}
    # One workspace hands out distinct timestamps; each fake counts from the same
    # base, so offset the second bot's counter.
    clients["gordy"]._ts += 1_000
    eng = SimulationEngine(
        agents=agents, slack_clients=clients, session_factory=factory, simulation_run_id=run_id,
    )
    # start() registers this hook; these tests drive _post_message without start().
    eng.message_log.set_persist_callback(eng._enqueue_persist)
    return eng


async def _contents(factory, run_id):
    async with factory() as db:
        return sorted((await db.execute(
            select(AgentMessage.content).where(AgentMessage.simulation_run_id == run_id)
        )).scalars().all())


async def test_a_post_returns_only_after_its_row_is_in_the_db(engine, monkeypatch, tmp_path):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        eng = _engine(monkeypatch, tmp_path, factory, run_id)
        ts = await eng._post_message("wang", "general", "durable at return")
        assert ts
        assert eng._pending_persist == []
        assert await _contents(factory, run_id) == ["durable at return"]
    finally:
        await _delete_run(factory, run_id)


async def test_concurrent_posts_are_each_persisted_once(engine, monkeypatch, tmp_path):
    """The gathered panel notes of one consult round post concurrently; neither may
    deadlock on the flush lock or duplicate."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        eng = _engine(monkeypatch, tmp_path, factory, run_id)
        first, second = await asyncio.wait_for(asyncio.gather(
            eng._post_message("wang", "general", "note one"),
            eng._post_message("gordy", "general", "note two"),
        ), timeout=30)
        assert first and second and first != second
        assert await _contents(factory, run_id) == ["note one", "note two"]
    finally:
        await _delete_run(factory, run_id)


async def test_a_failed_write_through_keeps_the_entry_and_the_post(monkeypatch, tmp_path):
    class _Down:
        def __call__(self):
            raise ConnectionRefusedError("database down")

    eng = _engine(monkeypatch, tmp_path, _Down(), uuid.uuid4())
    ts = await eng._post_message("wang", "general", "on Slack, not yet in the DB")
    assert ts, "the post still counts: it is on Slack"
    assert [e.content for e in eng._pending_persist] == ["on Slack, not yet in the DB"]
