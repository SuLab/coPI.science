"""Tests for AgentSlackClient's private-channel creation primitives.

``src/services/private_channels.py`` (the public-thread -> collab_private
channel migration service that used to be this file's subject) was deleted in
the 2026-08-12 removal-cycle consolidation sweep — decision 8 keeps
``collab_private`` as legacy-tolerance only, with no new creation path, and
the web reopen route (``POST /agent/{id}/proposals/{tid}/reopen``) has not
called this service since fix 9 (2026-08-12 final audit wave). What remains
worth pinning is ``AgentSlackClient.create_private_channel``/
``invite_to_channel`` themselves: general Slack-client capabilities (mock-mode
behaviour, name-collision retry, the 80-char cap) that are not specific to the
retired migration flow.
"""

import pytest

from src.agent.slack_client import AgentSlackClient


@pytest.fixture
def mock_client():
    """AgentSlackClient in mock mode (no real Slack)."""
    return AgentSlackClient(agent_id="su", bot_token="xoxb-placeholder-abc")


class TestCreatePrivateChannel:
    def test_returns_mock_channel_with_is_private(self, mock_client):
        ch = mock_client.create_private_channel("priv-test")
        assert ch is not None
        # Mock mode applies the same timestamp suffix as the live path.
        assert ch["name"].startswith("priv-test-")
        assert ch["is_private"] is True
        # Slack-off channels use the DB-native 'local:' id scheme.
        assert ch["id"].startswith("local:")

    def test_public_create_channel_still_works(self, mock_client):
        """Don't regress the existing create_channel behavior."""
        ch = mock_client.create_channel("general")
        assert ch is not None
        assert ch["name"] == "general"
        # Slack-off channels use the DB-native 'local:' id scheme.
        assert ch["id"] == "local:general"


class _FakeSlack:
    """Minimal stand-in for slack_sdk.WebClient.conversations_create.

    Raises name_taken for the first ``fail_times`` calls, then succeeds. Using
    a call counter (rather than a set of taken names) keeps the tests robust to
    the timestamp suffix, whose exact value isn't predictable.
    """

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []

    def conversations_create(self, name, is_private=False):
        from slack_sdk.errors import SlackApiError

        self.calls.append(name)
        if len(self.calls) <= self.fail_times:
            raise SlackApiError("name_taken", response={"error": "name_taken"})
        return {"channel": {"id": f"C_{name}", "name": name, "is_private": is_private}}


class TestCreatePrivateChannelNameTaken:
    """Regression: two channel-creation attempts with the same base slug (e.g.
    two proposals between the same agent pair in the same origin channel)
    yield an identical base slug; Slack rejects it with name_taken.
    create_private_channel disambiguates with a UTC timestamp suffix (plus
    random entropy on collision) rather than failing the caller."""

    _BASE = "priv-lairson-su-drug-repurposing"

    def _live_client(self, fail_times=0):
        client = AgentSlackClient(agent_id="su", bot_token="xoxb-real-token")
        client._client = _FakeSlack(fail_times)  # force out of mock mode
        return client

    def test_appends_timestamp_suffix(self):
        client = self._live_client()
        ch = client.create_private_channel(self._BASE)
        assert ch is not None
        # Base preserved, with a -YYYYMMDD-HHMMSS suffix appended.
        assert ch["name"].startswith(self._BASE + "-")
        assert ch["name"] != self._BASE
        # One API call in the common case — no probe-and-increment loop.
        assert len(client._client.calls) == 1

    def test_retries_with_entropy_on_name_taken(self):
        client = self._live_client(fail_times=1)
        ch = client.create_private_channel(self._BASE)
        assert ch is not None
        assert ch["name"].startswith(self._BASE + "-")
        # Two attempts: timestamp, then timestamp + entropy.
        calls = client._client.calls
        assert len(calls) == 2
        assert len(calls[1]) > len(calls[0])  # entropy makes the 2nd longer

    def test_returns_none_when_all_attempts_exhausted(self):
        client = self._live_client(fail_times=99)
        assert client.create_private_channel(self._BASE) is None

    def test_respects_slack_80_char_cap(self):
        client = self._live_client()
        long_base = "priv-" + ("x" * 100)
        ch = client.create_private_channel(long_base)
        assert ch is not None
        assert len(ch["name"]) <= 80


class TestInviteToChannel:
    def test_empty_invite_list_is_noop_true(self, mock_client):
        assert mock_client.invite_to_channel("C123", []) is True

    def test_mock_mode_returns_true(self, mock_client):
        assert mock_client.invite_to_channel("C123", ["U1", "U2", "BOT3"]) is True


class TestImports:
    def test_reopen_endpoint_imports(self):
        """Sanity: the endpoint module still imports cleanly now that its
        docstring no longer references the deleted migration service."""
        from src.routers.agent_page import reopen_proposal  # noqa: F401
