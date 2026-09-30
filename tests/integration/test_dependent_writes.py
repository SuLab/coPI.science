"""S2-06: a verdict or ThreadDecision commits only after its reply row is in the
DB; S1-12: a failed ThreadDecision write is retried, not lost."""
import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AgentMessage, OpportunityAssessment, SimulationRun, ThreadDecision
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


def _reply(n):
    v = {"subject_agent_id": f"lab{n}", "recommendation": "pass", "scores": {k: 2 for k in RUBRIC_WEIGHTS}}
    return f"<slack_message>⏸️ Closing {n}.</slack_message>\n<assessment_json>{json.dumps(v)}</assessment_json>"


async def _setup(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    labs = [Agent(f"lab{n}", f"Lab{n}Bot", f"Lab {n}", role="pi_lab") for n in (1, 2)]
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in [hub, *labs]}
    sim = SimulationEngine(agents=[hub, *labs], slack_clients=clients,
                           session_factory=factory, simulation_run_id=run.id)
    # `start()` registers this hook; these tests drive the units without it.
    sim.ctx.message_log.set_persist_callback(sim.persistence._enqueue_persist)
    return factory, run.id, sim, hub


async def _cleanup(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


def _arm_replies(sim, hub, monkeypatch, tids):
    """Stub the model and memory so `_reply_to_thread` posts one closing reply
    with a verdict sidecar per thread; returns the ThreadStates."""
    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    replies = {tid: _reply(n) for n, tid in tids}

    async def _gen(**kw):
        await asyncio.sleep(0)
        return replies[kw["log_meta"]["thread_ts"]]

    monkeypatch.setattr("src.agent.engine.deps.generate_with_tools", _gen)

    async def _no_memory(*a, **kw):
        return None

    monkeypatch.setattr(sim.memory, "_update_agent_memory", _no_memory)
    threads = []
    for n, tid in tids:
        t = ThreadState(thread_id=tid, channel="c", other_agent_id=f"lab{n}", has_pending_reply=True)
        hub.state.active_threads[tid] = t
        for i in range(7):
            sim.ctx.message_log.append(LogEntry(
                ts=tid if i == 0 else f"{tid}.{i}", channel="c",
                sender_agent_id=f"lab{n}" if i % 2 == 0 else "blackbird",
                sender_name="x", content=f"m{i}", thread_ts=None if i == 0 else tid,
                posted_at=float(i), slack_ts=tid if i == 0 else f"{tid}.{i}", slack_channel_id="C",
            ))
        threads.append(t)
    return threads


@pytest.mark.asyncio
async def test_each_verdict_commits_only_after_its_reply_row(engine, monkeypatch):
    factory, run_id, sim, hub = await _setup(engine)
    seen = []
    real_upsert = sim.verdicts.upsert

    async def _checked(thread, verdict, write_id, ordinal):
        async with factory() as db:
            present = await db.scalar(select(AgentMessage.id).where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.message_ts == verdict["slack_ts"],
            ))
        seen.append(present is not None)
        return await real_upsert(thread, verdict, write_id, ordinal)

    monkeypatch.setattr(sim.verdicts, "upsert", _checked)
    threads = _arm_replies(sim, hub, monkeypatch, ((1, "t1"), (2, "t2")))
    try:
        await asyncio.gather(*(sim.reply_lane._reply_to_thread(hub, t) for t in threads))
        assert seen == [True, True]
        async with factory() as db:
            assert len((await db.execute(select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id))).scalars().all()) == 2
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_verdict_waits_while_its_reply_row_is_unflushed(engine, monkeypatch):
    """The healthy path's write-through flush already lands the reply row, so the
    test above cannot tell the gate is there; this one holds the messages back."""
    factory, run_id, sim, hub = await _setup(engine)
    upserts = []
    real_upsert = sim.verdicts.upsert

    async def _counted(*a, **kw):
        upserts.append(1)
        return await real_upsert(*a, **kw)

    async def _stuck():
        return False

    monkeypatch.setattr(sim.verdicts, "upsert", _counted)
    monkeypatch.setattr(sim.persistence, "flush_before_dependent", _stuck)
    (thread,) = _arm_replies(sim, hub, monkeypatch, ((1, "t1"),))
    try:
        await sim.reply_lane._reply_to_thread(hub, thread)
        assert upserts == []
        assert [r["thread_id"] for r in sim.verdicts._pending_assessments] == ["t1"]
        await sim.verdicts._flush_pending_assessments(final=True)
        async with factory() as db:
            assert len((await db.execute(select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id))).scalars().all()) == 1
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_failed_decision_write_is_retried_by_the_next_flush(engine, monkeypatch):
    factory, run_id, sim, hub = await _setup(engine)
    calls = {"n": 0}
    real_write = sim.threads._write_decision

    async def _flaky(row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("QueuePool limit reached, connection timed out")
        await real_write(row)

    monkeypatch.setattr(sim.threads, "_write_decision", _flaky)

    async def _no_memory(*a, **kw):
        return None

    monkeypatch.setattr(sim.memory, "_update_agent_memory", _no_memory)
    thread = ThreadState(thread_id="t9", channel="c", other_agent_id="lab1")
    hub.state.active_threads["t9"] = thread
    try:
        await sim.threads._close_thread(hub, thread, "timeout")
        assert len(sim.threads._pending_decisions) == 1
        await sim.threads.flush_pending_decisions()
        assert sim.threads._pending_decisions == []
        async with factory() as db:
            rows = (await db.execute(select(ThreadDecision).where(
                ThreadDecision.simulation_run_id == run_id))).scalars().all()
        assert [r.thread_id for r in rows] == ["t9"]
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_decision_waits_while_messages_are_unflushed(engine, monkeypatch):
    factory, run_id, sim, hub = await _setup(engine)

    async def _stuck():
        return False

    monkeypatch.setattr(sim.persistence, "flush_before_dependent", _stuck)

    async def _no_memory(*a, **kw):
        return None

    monkeypatch.setattr(sim.memory, "_update_agent_memory", _no_memory)
    thread = ThreadState(thread_id="t8", channel="c", other_agent_id="lab1")
    hub.state.active_threads["t8"] = thread
    try:
        await sim.threads._close_thread(hub, thread, "timeout")
        async with factory() as db:
            assert (await db.execute(select(ThreadDecision).where(
                ThreadDecision.simulation_run_id == run_id))).scalars().all() == []
        assert len(sim.threads._pending_decisions) == 1
        await sim.threads.flush_pending_decisions(final=True)
        async with factory() as db:
            assert len((await db.execute(select(ThreadDecision).where(
                ThreadDecision.simulation_run_id == run_id))).scalars().all()) == 1
    finally:
        await _cleanup(factory, run_id)
