import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.models import AgentRegistry, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


async def _setup(engine, monkeypatch, tmp_path, *, status):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        db.add(AgentRegistry(agent_id="hubx", bot_name="HubBot", pi_name="Hub", role="scout_hub",
                             status=status))
        await db.commit()
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("hubx", "HubBot", "Hub", role="scout_hub")
    sim = SimulationEngine(agents=[hub], slack_clients={"hubx": FakeSlackClient(agent_id="hubx")},
                           session_factory=factory, simulation_run_id=run.id)

    async def _gen(**kw):
        return "new memory"

    monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _gen)
    return factory, run.id, sim, hub


async def _cleanup(factory, run_id):
    async with factory() as db:
        await db.delete(await db.get(SimulationRun, run_id))
        await db.execute(AgentRegistry.__table__.delete().where(AgentRegistry.agent_id == "hubx"))
        await db.commit()


@pytest.mark.asyncio
async def test_a_deactivated_agent_gets_no_memory_file(engine, monkeypatch, tmp_path):
    factory, run_id, sim, hub = await _setup(engine, monkeypatch, tmp_path, status="inactive")
    try:
        await sim.memory._update_agent_memory(hub, "Thread closed: timeout")
        assert not (tmp_path / "memory" / "hubx" / "public.md").exists()
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_the_hub_without_a_user_still_gets_its_memory(engine, monkeypatch, tmp_path):
    factory, run_id, sim, hub = await _setup(engine, monkeypatch, tmp_path, status="active")
    try:
        await sim.memory._update_agent_memory(hub, "Thread closed: timeout")
        assert (tmp_path / "memory" / "hubx" / "public.md").read_text(encoding="utf-8") == "new memory\n"
    finally:
        await _cleanup(factory, run_id)
