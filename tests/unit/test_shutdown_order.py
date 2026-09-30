import asyncio

import pytest

from src.agent.agent import Agent
from src.agent.engine.constants import MEMORY_EVENTS_MAX_AT_SHUTDOWN
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


@pytest.mark.asyncio
async def test_a_slow_drain_cannot_delay_the_sweep_and_the_drain_is_unchanged(monkeypatch):
    sim = SimulationEngine(agents=[Agent("a", "ABot", "A")], slack_clients={"a": FakeSlackClient(agent_id="a")})
    order: list[str] = []

    async def _sweep(end_reason):
        order.append("sweep")

    async def _drain(limit=None):
        order.append(f"drain:{limit}")
        await asyncio.sleep(0.05)
        return 0

    async def _flush(*a, final=False, **kw):
        order.append(f"flush:{'final' if final else 'first'}")

    async def _flush_decisions(*, final=False):
        order.append(f"decisions:{'final' if final else 'first'}")

    monkeypatch.setattr(sim.headlines, "shutdown_sweep", _sweep)
    monkeypatch.setattr(sim.memory, "_drain_memory_events", _drain)
    monkeypatch.setattr(sim.persistence, "_flush_persisted", _flush)
    monkeypatch.setattr(sim.threads, "flush_pending_decisions", _flush_decisions)
    await sim.stop()

    assert order.index("flush:first") < order.index("decisions:first") < order.index("sweep")
    assert order.index("sweep") < order.index(f"drain:{MEMORY_EVENTS_MAX_AT_SHUTDOWN}")
    assert order.index(f"drain:{MEMORY_EVENTS_MAX_AT_SHUTDOWN}") < order.index("flush:final")
    assert order.index("decisions:final") > order.index("sweep")
