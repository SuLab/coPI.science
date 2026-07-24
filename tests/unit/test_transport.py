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

    def test_cache_channel_ids_seeds_the_lookup(self):
        t = NullTransport("su")
        t.cache_channel_ids({"general": "local:general", "funding": "C123"})
        assert t.get_channel_id("general") == "local:general"
        assert t.list_channels()["funding"] == "C123"

    def test_dead_visibility_setter_removed(self):
        # set_visibility_lookup had 0 callers — dead code, dropped (M2).
        assert not hasattr(NullTransport("su"), "set_visibility_lookup")
        from src.agent.slack_client import AgentSlackClient
        assert not hasattr(AgentSlackClient, "set_visibility_lookup")


class TestChannelCacheContract:
    """M2: the channel name→id write path is part of the declared Transport
    contract, so the engine seeds it via a public method rather than poking a
    private ``_channel_name_to_id`` attribute that a new backend need not have."""

    def test_cache_channel_ids_is_declared_on_the_protocol(self):
        assert hasattr(Transport, "cache_channel_ids")
        from src.agent.slack_client import AgentSlackClient
        assert hasattr(AgentSlackClient, "cache_channel_ids")

    def test_backend_without_private_attr_can_be_seeded(self):
        # Before M2 the engine crashed with AttributeError on a Protocol-only
        # backend. Now the seed goes through cache_channel_ids, which any
        # conforming backend implements however it likes.
        class StubBackend:
            def __init__(self):
                self._seeded: dict[str, str] = {}

            def cache_channel_ids(self, mapping: dict[str, str]) -> None:
                self._seeded.update(mapping)

            def get_channel_id(self, name: str) -> str | None:
                return self._seeded.get(name)

        b = StubBackend()
        assert not hasattr(b, "_channel_name_to_id")
        b.cache_channel_ids({"general": "C1"})
        assert b.get_channel_id("general") == "C1"
