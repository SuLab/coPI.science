"""A `agents.role` change on a live agent is picked up without a process restart.

`_sync_roster_from_db` (src/agent/simulation.py) polls `AgentRegistry` on a timer to add
newly-activated agents and remove deactivated ones. Before this test existed, a role
change on a *surviving* agent (still active, still present in the roster) was invisible:
the method computed `to_add`/`to_remove` and returned early whenever both were empty,
which is exactly the case for a pure role reassignment. This exercises that path with a
real Postgres: seed one active agent at role 'pi_lab', update its DB row to 'scout_hub'
in a separate committed session (mirroring how the admin UI would write it), force the
poll throttle open, and assert the in-memory `Agent.role` picks up the change on the next
sync tick.

Not using the rolled-back `db_session` fixture on purpose: the engine opens its own
sessions via `session_factory` and the test needs its own UPDATE to be visible to those
sessions the way it would be in production (two independent connections, both committed),
not merely visible within one shared, uncommitted transaction.
"""

import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.transport import NullTransport
from src.models import AgentRegistry, SimulationRun

pytestmark = pytest.mark.integration

AGENT_ID = "role-flip-test-agent"


@pytest.fixture
async def engine_with_one_agent(engine):
    """A real `SimulationEngine` wired to the migrated test Postgres, with one
    active `AgentRegistry` row seeded at role='pi_lab'.

    Slack is off (NullTransport) — this test is about the roster/role sync, not
    transport. Yields `(sim_engine, session_factory, agent_id)`.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        db.add(AgentRegistry(
            agent_id=AGENT_ID,
            bot_name="RoleFlipTestBot",
            pi_name="PI Role Flip",
            status="active",
            role="pi_lab",
        ))
        await db.commit()

    sim_engine = SimulationEngine(
        agents=[Agent(agent_id=AGENT_ID, bot_name="RoleFlipTestBot",
                       pi_name="PI Role Flip", role="pi_lab")],
        slack_clients={AGENT_ID: NullTransport(AGENT_ID)},
        budget_cap=0,
        session_factory=factory,
        simulation_run_id=run_id,
        slack_enabled=False,
    )

    try:
        yield sim_engine, factory, AGENT_ID
    finally:
        async with factory() as db:
            await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id == AGENT_ID))
            await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
            await db.commit()


async def test_role_change_is_picked_up_without_restart(engine_with_one_agent):
    """A DB role change on a running agent updates Agent.role on the next sync."""
    engine, session_factory, agent_id = engine_with_one_agent
    assert engine.agents[agent_id].role == "pi_lab"

    async with session_factory() as db:
        await db.execute(
            update(AgentRegistry).where(AgentRegistry.agent_id == agent_id).values(role="scout_hub")
        )
        await db.commit()

    engine._last_roster_poll = 0.0  # force the throttle open
    await engine._sync_roster_from_db()

    assert engine.agents[agent_id].role == "scout_hub"

    # Sanity check against the DB directly, so a false pass (e.g. the sync silently
    # no-oping and the assertion above passing only because nothing ever changed
    # `.role` back) can't hide behind the in-memory assertion alone.
    async with session_factory() as db:
        row = (await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
        )).scalar_one()
        assert row.role == "scout_hub"
