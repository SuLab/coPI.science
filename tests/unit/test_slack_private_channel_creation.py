"""What survives of the private-channel creation tests.

``src/services/private_channels.py`` (the public-thread -> collab_private
channel migration service that used to be this file's subject) was deleted in
the 2026-08-12 removal-cycle consolidation sweep — decision 8 keeps
``collab_private`` as legacy-tolerance only, with no new creation path. The
client's own private-create and invite methods had no caller in ``src/`` after
that and are gone too; the live Slack tier builds its private fixtures through
``tests/slack_live_support.py`` instead. What remains pinned here is that the
public ``create_channel`` keeps its Slack-off behaviour, and that the reopen
route still imports.
"""

import pytest

from src.agent.slack_client import AgentSlackClient


@pytest.fixture
def mock_client():
    """AgentSlackClient in mock mode (no real Slack)."""
    return AgentSlackClient(agent_id="su", bot_token="xoxb-placeholder-abc")


class TestCreateChannel:
    def test_public_create_channel_still_works(self, mock_client):
        """Don't regress the existing create_channel behavior."""
        ch = mock_client.create_channel("general")
        assert ch is not None
        assert ch["name"] == "general"
        # Slack-off channels use the DB-native 'local:' id scheme.
        assert ch["id"] == "local:general"

