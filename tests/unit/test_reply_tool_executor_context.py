from types import SimpleNamespace

from src.agent.agent import Agent
from src.agent.engine import reply_lane
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState


def _engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Jane Wang")
    return SimulationEngine(agents=[hub, lab], slack_clients={}), hub, lab


async def test_the_executor_passes_gate_hub_ids_and_own_ids(monkeypatch):
    eng, _hub, lab = _engine()
    lab.allowed_sender_ids = {"blackbird"}
    lab._own_paper_ids = {"38980071"}
    seen = {}

    async def fake_execute_tool(name, tool_input, agent_id, thread, **kwargs):
        seen.update(kwargs, agent_id=agent_id)
        return "ok"

    monkeypatch.setattr(reply_lane, "execute_tool", fake_execute_tool)
    thread = ThreadState(thread_id="t", channel="general", other_agent_id="blackbird")
    executor = eng._reply_tool_executor(lab, thread)
    assert await executor("retrieve_profile", {"agent_id": "blackbird"}) == "ok"
    assert seen["agent_id"] == "wang" and seen["role"] == "pi_lab"
    assert seen["hub_agent_ids"] == {"blackbird": "scout_hub"}
    assert seen["allowed_sender_ids"] == {"blackbird"}
    assert seen["own_paper_ids"] == {"38980071"}


async def test_the_preflight_history_carries_each_sender_agent_id():
    eng, hub, _lab = _engine()
    root = "1600000100.000000"
    for ts, sender, name, parent in (
        (root, "wang", "WangBot", None),
        ("1600000101.000000", "blackbird", "BlackbirdBot", root),
    ):
        eng.message_log.append(LogEntry(
            ts=ts, channel="general", sender_agent_id=sender, sender_name=name,
            content=f"from {sender}", thread_ts=parent, posted_at=float(ts),
        ))
    thread = ThreadState(thread_id=root, channel="general", other_agent_id="wang")
    history = await eng._reply_preflight(hub, thread, SimpleNamespace(max_thread_messages=12))
    assert [h["sender_agent_id"] for h in history] == ["wang", "blackbird"]
