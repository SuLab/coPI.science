"""§8.2 through the engine: claim race, in doubt, hold filter, carry-forward."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import OpportunityAssessment, SimulationRun, ThreadDecision
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


class _Client(FakeSlackClient):
    def __init__(self, *a, raise_on_post=False, refuse=False, **kw):
        super().__init__(*a, **kw)
        self.raise_on_post = raise_on_post
        self.refuse = refuse
        self.headlines = []

    async def apost_message(self, channel, text, thread_ts=None, **kw):
        if channel == "assessments-summary":
            if self.raise_on_post:
                raise TimeoutError("read timed out")
            if self.refuse:
                return None
            self.headlines.append(text)
        return await super().apost_message(channel, text, thread_ts=thread_ts)

    async def aget_permalink(self, *a, **kw):
        return None


def _reply(score, closing):
    verdict = {"subject_agent_id": "gordy", "recommendation": "advance",
               "scores": {k: score for k in RUBRIC_WEIGHTS}, "company_or_project": "P",
               "elevator_pitch": "E."}
    body = "⏸️ Closing." if closing else "Noted."
    return f"<slack_message>{body}</slack_message>\n<assessment_json>{json.dumps(verdict)}</assessment_json>"


async def _engine(engine, client):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("gordy", "GordyBot", "Gordy", role="pi_lab")
    sim = SimulationEngine(agents=[hub, lab], slack_clients={"blackbird": client, "gordy": FakeSlackClient(agent_id="gordy")},
                           session_factory=factory, simulation_run_id=run.id)
    sim.channel_directory._assessments_summary_channel_id = "C_SUMMARY"
    return factory, run.id, sim, hub


async def _cleanup(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


@pytest.mark.asyncio
async def test_a_terminal_verdict_superseded_by_another_terminal_is_announced_once(engine):
    client = _Client(agent_id="blackbird")
    factory, run_id, sim, hub = await _engine(engine, client)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy", message_count=11)
    try:
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(3, False), "1.1", closes_thread=False)
        thread.message_count = 13
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(4, False), "2.2", closes_thread=False)
        assert len(client.headlines) == 1
        async with factory() as db:
            (row,) = (await db.execute(select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id))).scalars().all()
        assert row.summary_posted_at is not None and row.verdict_revision == 2
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_transport_error_leaves_the_claim_in_doubt_and_is_never_reposted(engine):
    client = _Client(agent_id="blackbird", raise_on_post=True)
    factory, run_id, sim, hub = await _engine(engine, client)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy", message_count=11)
    try:
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(3, False), "1.1", closes_thread=False)
        client.raise_on_post = False
        assert await sim.headlines._announce_owed_headline("t1", trigger="shutdown") is False
        assert client.headlines == []
        async with factory() as db:
            (row,) = (await db.execute(select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id))).scalars().all()
        assert row.summary_claimed_at is not None and row.summary_posted_at is None
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_definite_refusal_releases_the_claim(engine):
    client = _Client(agent_id="blackbird", refuse=True)
    factory, run_id, sim, hub = await _engine(engine, client)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy", message_count=11)
    try:
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(3, False), "1.1", closes_thread=False)
        async with factory() as db:
            (row,) = (await db.execute(select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id))).scalars().all()
        assert row.summary_claimed_at is None and row.summary_posted_at is None
        client.refuse = False
        assert await sim.headlines._announce_owed_headline("t1", trigger="thread-close") is True
        assert len(client.headlines) == 1
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_hold_sweep_announces_only_ended_interviews_and_today_announces_all(engine):
    client = _Client(agent_id="blackbird")
    factory, run_id, sim, hub = await _engine(engine, client)
    try:
        for tid in ("open", "ended"):
            t = ThreadState(thread_id=tid, channel="c", other_agent_id="gordy", message_count=7)
            await sim.verdicts._capture_hub_assessment(hub, t, _reply(3, False), f"{tid}.1", closes_thread=False)
        async with factory() as db:
            db.add(ThreadDecision(simulation_run_id=run_id, thread_id="ended", channel="c",
                                  agent_a="blackbird", agent_b="gordy", outcome="timeout"))
            await db.commit()
        await sim.headlines.shutdown_sweep("operator_hold")
        assert len(client.headlines) == 1
        await sim.headlines.shutdown_sweep("signal")
        assert len(client.headlines) == 2, "TODAY announces the open interview too, as today"
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_natural_end_announces_open_interviews_too(engine):
    client = _Client(agent_id="blackbird")
    factory, run_id, sim, hub = await _engine(engine, client)
    try:
        t = ThreadState(thread_id="open", channel="c", other_agent_id="gordy", message_count=7)
        await sim.verdicts._capture_hub_assessment(hub, t, _reply(3, False), "open.1", closes_thread=False)
        await sim.headlines.shutdown_sweep("time_limit")
        assert len(client.headlines) == 1, "FINALIZE announces every owed headline"
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_claim_race_gives_one_post(engine):
    import asyncio

    client = _Client(agent_id="blackbird")
    factory, run_id, sim, hub = await _engine(engine, client)
    try:
        t = ThreadState(thread_id="race", channel="c", other_agent_id="gordy", message_count=7)
        await sim.verdicts._capture_hub_assessment(hub, t, _reply(3, False), "race.1", closes_thread=False)
        results = await asyncio.gather(
            sim.headlines._announce_owed_headline("race", trigger="thread-close"),
            sim.headlines._announce_owed_headline("race", trigger="shutdown"),
        )
        assert sorted(results) == [False, True]
        assert len(client.headlines) == 1
    finally:
        await _cleanup(factory, run_id)
