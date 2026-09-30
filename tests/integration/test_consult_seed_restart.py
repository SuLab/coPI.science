import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import SimulationRun, SpecialistConsult
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_consults_from_before_a_restart_count_even_when_memory_holds_one(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        for domain in ("clinical", "regulatory"):
            db.add(SpecialistConsult(simulation_run_id=run.id, agent_id="blackbird",
                                     subject_agent_id="gordy", thread_id="t1", domain=domain,
                                     question="q", truncated=False))
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
                           session_factory=factory, simulation_run_id=run.id)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy")
    sim.panel._record_consult("gordy", "chemistry", "t1")   # post-restart consult in memory
    verdict = {"subject_agent_id": "gordy", "recommendation": "advance",
               "scores": {k: 4 for k in RUBRIC_WEIGHTS}}
    try:
        await sim.panel._seed_consults_from_db(verdict, thread)
        assert sim.panel._consulted_domains("gordy", "t1") >= {"clinical", "regulatory", "chemistry"}
        assert thread.floor_armed is True
        sim.panel._specialist_consults[("gordy", "t1")].discard("clinical")
        await sim.panel._seed_consults_from_db(verdict, thread)
        assert "clinical" not in sim.panel._consulted_domains("gordy", "t1"), "seeded once per process"
    finally:
        async with factory() as db:
            await db.delete(await db.get(SimulationRun, run.id))
            await db.commit()


@pytest.mark.asyncio
async def test_a_seed_that_adds_nothing_does_not_arm_the_floor(engine):
    """Normal path: memory already holds every DB consult, so the merge is a
    no-op and floor_armed keeps whatever the turn latched (B24)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        db.add(SpecialistConsult(simulation_run_id=run.id, agent_id="blackbird",
                                 subject_agent_id="gordy", thread_id="t1", domain="clinical",
                                 question="q", truncated=False))
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
                           session_factory=factory, simulation_run_id=run.id)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy")
    thread.floor_armed = False
    sim.panel._record_consult("gordy", "clinical", "t1")
    verdict = {"subject_agent_id": "gordy", "recommendation": "advance",
               "scores": {k: 4 for k in RUBRIC_WEIGHTS}}
    try:
        await sim.panel._seed_consults_from_db(verdict, thread)
        assert thread.floor_armed is False
    finally:
        async with factory() as db:
            await db.delete(await db.get(SimulationRun, run.id))
            await db.commit()
