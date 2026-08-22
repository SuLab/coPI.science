"""The assessments-summary channel is created and joined by the hub only,
and stays out of the topical channel-discovery machinery (design D11)."""
import pytest

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import SlackListingIncomplete
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


async def test_ensure_assessments_summary_channel_adopts_existing_channel(
    monkeypatch, tmp_path,
):
    """Channel already exists in Slack; should adopt it without creating again."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    # Pre-populate the channel in hub_client's channel listing
    hub_client = FakeSlackClient(
        agent_id="blackbird",
        existing_channels={ASSESSMENTS_SUMMARY_CHANNEL: f"C_{ASSESSMENTS_SUMMARY_CHANNEL}"}
    )
    lab_client = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client},
    )

    eng._ensure_assessments_summary_channel()

    # Should adopt the existing channel, NOT create a new one
    assert len(hub_client.created_channels) == 0
    assert eng._assessments_summary_channel_id == f"C_{ASSESSMENTS_SUMMARY_CHANNEL}"
    assert ASSESSMENTS_SUMMARY_CHANNEL in eng._channel_id_map
    assert eng._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] == f"C_{ASSESSMENTS_SUMMARY_CHANNEL}"
    # Should still join the hub
    assert ASSESSMENTS_SUMMARY_CHANNEL in hub_client.joined_channels
    # Lab should NOT join
    assert ASSESSMENTS_SUMMARY_CHANNEL not in lab_client.joined_channels


async def test_ensure_assessments_summary_channel_returns_early_on_listing_incomplete(
    monkeypatch, tmp_path,
):
    """When list_channels() raises SlackListingIncomplete, should return early."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub_client = FakeSlackClient(agent_id="blackbird")

    # Patch list_channels to raise SlackListingIncomplete
    def raise_incomplete(*args, **kwargs):
        raise SlackListingIncomplete(method="list_channels", partial=[], reason="test_timeout")

    monkeypatch.setattr(hub_client, "list_channels", raise_incomplete)

    lab_client = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client},
    )

    # Should not raise, should return early
    eng._ensure_assessments_summary_channel()

    # Should NOT have created or adopted anything
    assert eng._assessments_summary_channel_id is None
    assert ASSESSMENTS_SUMMARY_CHANNEL not in eng._channel_id_map


async def test_ensure_assessments_summary_channel_local_mode_fallback(
    monkeypatch, tmp_path,
):
    """When Slack is disconnected, should use local: prefix fallback."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")

    # Create a disconnected client (is_connected returns False)
    class DisconnectedFakeSlackClient(FakeSlackClient):
        @property
        def is_connected(self) -> bool:
            return False

    hub_client = DisconnectedFakeSlackClient(agent_id="blackbird")
    lab_client = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client},
    )

    eng._ensure_assessments_summary_channel()

    # Should use local: prefix
    expected_id = f"local:{ASSESSMENTS_SUMMARY_CHANNEL}"
    assert eng._assessments_summary_channel_id == expected_id
    assert ASSESSMENTS_SUMMARY_CHANNEL in eng._channel_id_map
    assert eng._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] == expected_id
    # Should NOT have tried to create channel or join
    assert len(hub_client.created_channels) == 0
    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.joined_channels
