"""The engine carries no DB inbound poll, proposal state or private-channel sync."""

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import AgentState

RETIRED = (
    "_poll_inbound_from_db", "_seed_pi_inbox_cursor", "_pi_inbox_cursor",
    "_sync_private_channels_from_db", "_rewind_cursors_for_private_channels",
    "_private_channel_members", "_finalized_private_channels",
)


def test_the_engine_has_no_retired_members():
    eng = SimulationEngine(agents=[Agent("su", "SuBot", "PI su")], slack_clients={})
    present = [name for name in RETIRED if hasattr(eng, name)]
    assert present == []


def test_agent_state_has_no_pending_proposals():
    assert "pending_proposals" not in AgentState.__dataclass_fields__


def test_client_for_channel_always_answers_the_fallback():
    eng = SimulationEngine(agents=[Agent("su", "SuBot", "PI su")], slack_clients={})
    sentinel = object()
    assert eng._client_for_channel("G_anything", sentinel) is sentinel
