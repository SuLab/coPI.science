"""AG-5: the run row's totals come from the final flush's COUNT, not from the
in-process counters a resume starts from zero."""
import ast
import inspect
import textwrap

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.models import AgentMessage, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


def test_run_simulation_no_longer_overwrites_the_totals():
    from src.agent import main as agent_main

    tree = ast.parse(textwrap.dedent(inspect.getsource(agent_main._run_simulation_locked)))
    targets = {
        t.attr for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Attribute)
    }
    assert "total_messages" not in targets and "total_api_calls" not in targets


@pytest.mark.asyncio
async def test_a_resumed_runs_total_messages_equals_the_row_count(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    a = Agent("a", "ABot", "A")
    sim = SimulationEngine(agents=[a], slack_clients={"a": FakeSlackClient(agent_id="a")},
                           session_factory=factory, simulation_run_id=run.id)
    sim.ctx.message_log.set_persist_callback(sim.persistence._enqueue_persist)
    for i in range(5):
        sim.ctx.message_log.append(LogEntry(ts=f"{i}.0", channel="c", sender_agent_id="a",
                                            sender_name="ABot", content=f"m{i}", thread_ts=None,
                                            posted_at=float(i), is_bot=True))
    a.message_count = 2   # a resumed process only counts its own messages
    try:
        await sim.stop()
        async with factory() as db:
            row = await db.get(SimulationRun, run.id)
            from sqlalchemy import func, select
            count = await db.scalar(select(func.count(AgentMessage.id)).where(
                AgentMessage.simulation_run_id == run.id))
        assert row.total_messages == count == 5
    finally:
        async with factory() as db:
            await db.delete(await db.get(SimulationRun, run.id))
            await db.commit()


@pytest.mark.asyncio
async def test_stop_refreshes_the_totals_after_step_one_emptied_the_buffer(engine, monkeypatch):
    """The final flush finds the buffer empty and the refresh throttled, and must
    still write COUNT(agent_messages) and the live call sum, the shutdown memory
    drain's calls included."""
    import time

    from sqlalchemy import func, select

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    a = Agent("a", "ABot", "A")
    sim = SimulationEngine(agents=[a], slack_clients={"a": FakeSlackClient(agent_id="a")},
                           session_factory=factory, simulation_run_id=run.id)
    sim.ctx.message_log.set_persist_callback(sim.persistence._enqueue_persist)
    for i in range(3):
        sim.ctx.message_log.append(LogEntry(ts=f"{i}.0", channel="c", sender_agent_id="a",
                                            sender_name="ABot", content=f"m{i}", thread_ts=None,
                                            posted_at=float(i), is_bot=True))
    a.api_call_count = 5
    sim.persistence._last_run_stats_update = time.time()

    async def _drain_books_a_call(limit=None):
        a.api_call_count += 2

    monkeypatch.setattr(sim, "_drain_memory_events", _drain_books_a_call)
    try:
        await sim.stop()
        async with factory() as db:
            row = await db.get(SimulationRun, run.id)
            count = await db.scalar(select(func.count(AgentMessage.id)).where(
                AgentMessage.simulation_run_id == run.id))
        assert count == 3
        assert row.total_messages == count
        assert row.total_api_calls == 7 == a.api_call_count
    finally:
        async with factory() as db:
            await db.delete(await db.get(SimulationRun, run.id))
            await db.commit()
