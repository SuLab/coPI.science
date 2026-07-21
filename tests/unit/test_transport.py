"""Tests for the message transport abstraction (Slack-off mode)."""

from src.agent.transport import NullTransport, Transport


class TestNullTransport:
    def test_conforms_to_protocol(self):
        t = NullTransport("su")
        assert isinstance(t, Transport)

    def test_reports_disconnected(self):
        t = NullTransport("su")
        # is_connected=False makes the engine's guards take the no-op path.
        assert t.is_connected is False
        assert t.bot_user_id is None
        assert t.connect() is True  # usable, just not Slack-backed

    def test_outbound_posts_are_noops(self):
        t = NullTransport("su")
        # No Slack ts — the engine mints a local canonical id instead.
        assert t.post_message("general", "hi") is None
        assert t.send_dm("U1", "hi") is None
        assert t.open_dm_channel("U1") is None

    def test_channel_creates_use_local_ids(self):
        t = NullTransport("su")
        assert t.create_channel("general") == {"id": "local:general", "name": "general"}
        priv = t.create_private_channel("priv-a-b")
        assert priv["id"] == "local:priv-a-b"
        assert priv["is_private"] is True
        assert t.invite_to_channel("local:x", ["U1", "U2"]) is True
        assert t.join_channel("local:x") is None

    def test_inbound_polls_return_empty(self):
        t = NullTransport("su")
        assert t.poll_channel_messages("local:general") == []
        assert t.get_thread_replies("local:general", "1.0") == []
        assert t.get_full_channel_history("local:general") == []
        assert t.get_all_thread_replies("local:general", "1.0") == []
        assert t.poll_dm_messages("U1") == []

    def test_slack_client_conforms_to_protocol(self):
        # The real client must structurally satisfy the same Protocol.
        from src.agent.slack_client import AgentSlackClient
        client = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
        assert isinstance(client, Transport)
