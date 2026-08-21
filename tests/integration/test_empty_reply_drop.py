"""E1: consecutive empty replies abandon an interview; the abandonment must leave a row.

A single empty reply is NOT a loss: `has_pending_reply` stays True and the next
Phase-4 pass retries the same ordinal. The loss happens at the back-off
(`empty_response_count >= 2`), which permanently strands the thread — at ANY
ordinal, not just CONCLUDE: run 076e80b6 stranded a thread at message count 2.
Setup mirrors `_drive_reply` in test_hub_assessment_capture_gate.py.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration

_CONCLUDE_COUNT = 11  # prior count 11 -> ordinal 12, the CONCLUDE turn
_EXPLORE_COUNT = 1    # prior count 1  -> ordinal 2, the measured abandonment


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _drops(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AssessmentDrop)
            .where(AssessmentDrop.simulation_run_id == run_id)
            .order_by(AssessmentDrop.created_at)
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


async def _stranded_thread(engine, monkeypatch, *, prior_messages, role="scout_hub"):
    """An engine + thread ready for `_reply_to_thread`, with the model scripted
    to return NOTHING. Returns before driving, so a test can drive the same
    thread twice — the back-off needs two consecutive empties."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role=role)
    sim = SimulationEngine(
        agents=[agent],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    thread = ThreadState(
        thread_id="t1", channel="single-cell-omics", other_agent_id="gordy",
        message_count=prior_messages, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    for i in range(prior_messages):
        ts = "t1" if i == 0 else f"t1.{i}"
        sim.message_log.append(LogEntry(
            ts=ts, channel="single-cell-omics",
            sender_agent_id="gordy" if i % 2 == 0 else "blackbird",
            sender_name="GordyBot" if i % 2 == 0 else "BlackbirdBot",
            content=f"prior interview message {i}",
            thread_ts=None if i == 0 else "t1",
            posted_at=float(i), slack_ts=ts, slack_channel_id="C_OMICS",
        ))
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _empty_reply(**kwargs):
        return ""

    monkeypatch.setattr(
        "src.agent.simulation.generate_with_tools", _empty_reply
    )
    return sim, agent, thread, factory, run_id


async def test_two_empty_replies_at_conclude_record_one_drop(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        rows = await _drops(factory, run_id)
        assert [r.reason for r in rows] == ["empty_reply"], (
            "one abandonment, one row — not one per empty reply"
        )
        assert rows[0].subject_agent_id == "gordy"
        assert rows[0].thread_id == "t1"
        assert thread.has_pending_reply is False, "the back-off must still fire"
    finally:
        await _delete_run(factory, run_id)


async def test_a_single_empty_reply_is_retried_not_recorded(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)

        assert await _drops(factory, run_id) == []
        assert thread.has_pending_reply is True, (
            "one empty reply is retryable — recording it would be a false loss"
        )
    finally:
        await _delete_run(factory, run_id)


async def test_a_mid_interview_abandonment_is_also_recorded(engine, monkeypatch):
    # The measured incident class: run 076e80b6 stranded a thread at count=2.
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_EXPLORE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        rows = await _drops(factory, run_id)
        assert [r.reason for r in rows] == ["empty_reply"]
    finally:
        await _delete_run(factory, run_id)


async def test_a_lab_agent_back_off_records_no_drop(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT, role="pi_lab",
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        assert await _drops(factory, run_id) == [], (
            "labs never owe a verdict; their back-off is not an assessment loss"
        )
    finally:
        await _delete_run(factory, run_id)
