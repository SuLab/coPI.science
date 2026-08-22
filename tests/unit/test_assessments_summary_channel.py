"""The assessments-summary channel is created and joined by the hub only,
and stays out of the topical channel-discovery machinery (design D11)."""
import pytest

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def test_assessments_summary_channel_is_not_a_seeded_channel():
    assert ASSESSMENTS_SUMMARY_CHANNEL not in SEEDED_CHANNELS


def test_assessments_summary_channel_is_not_a_discovery_keyword_channel():
    from src.agent.simulation import _CHANNEL_KEYWORDS, _UNIVERSAL_CHANNELS
    assert ASSESSMENTS_SUMMARY_CHANNEL not in _CHANNEL_KEYWORDS
    assert ASSESSMENTS_SUMMARY_CHANNEL not in _UNIVERSAL_CHANNELS


async def test_ensure_assessments_summary_channel_creates_and_joins_only_the_hub(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub_client = FakeSlackClient(agent_id="blackbird")
    lab_client = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client},
    )

    eng._ensure_assessments_summary_channel()

    assert eng._assessments_summary_channel_id is not None
    assert ASSESSMENTS_SUMMARY_CHANNEL in eng._channel_id_map
    assert ASSESSMENTS_SUMMARY_CHANNEL in hub_client.joined_channels
    assert ASSESSMENTS_SUMMARY_CHANNEL not in lab_client.joined_channels
