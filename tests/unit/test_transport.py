"""Tests for the message transport abstraction (Slack-off mode).

The second class covers the *outbound* half of the declared contract: `post_message`
reports one record per message the backend really created, and the engine writes one
`agent_messages` row per record. Both halves are needed and neither is observable from
inside our own database with `NullTransport`, which never splits — so a mirror that
recorded one row for a post the backend turned into five looked identical here (Rule S2)
and only showed up as messages in Slack with no row.
"""

import asyncio

from src.agent.simulation import SimulationEngine
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


class _SplittingTransport:
    """A backend that reports what it really posted, per the declared contract.

    Deliberately not `FakeSlackClient`: that one always answers with a single ts, so it
    cannot express "this text became three messages" — which is the case that lost four
    rows out of five in production.
    """

    def __init__(self, chunks_per_post: int = 1):
        self.agent_id = "su"
        self.chunks_per_post = chunks_per_post
        self.calls: list[dict] = []
        self._n = 1_700_000_000

    def connect(self) -> bool:
        return True

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def bot_user_id(self) -> str | None:
        return "U_SU"

    def post_message(self, channel, text, thread_ts=None):
        self.calls.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        posted = []
        for index in range(self.chunks_per_post):
            self._n += 1
            ts = f"{self._n}.000000"
            parent = thread_ts if (thread_ts or index == 0) else posted[0]["ts"]
            posted.append({
                "ts": ts, "channel": f"C_{channel}",
                "text": f"{text}#{index}", "thread_ts": parent,
            })
        return {**posted[0], "posted_messages": posted}

    async def apost_message(self, *args, **kwargs):
        return await asyncio.to_thread(self.post_message, *args, **kwargs)

    def _resolve_channel_id(self, channel):
        return channel if channel.startswith(("C", "G")) else f"C_{channel}"


class TestPostResultContract:
    """`post_message` -> `posted_messages` -> one row per message, in bijection."""

    def _engine(self, transport):
        from src.agent.agent import Agent

        return SimulationEngine(agents=[Agent("su", "SuBot", "Andrew Su")],
                                slack_clients={"su": transport})

    # --- the normaliser, in isolation -------------------------------------------

    def test_nothing_posted_reports_no_messages(self):
        """Which is the signal to mint a local canonical id instead."""
        assert SimulationEngine._mirrored_messages(None, "text", None) == []

    def test_a_backend_that_never_splits_may_omit_the_key(self):
        """`NullTransport` and any simple backend report a bare result; it describes the
        one message it made, and the source text is what that message carries."""
        out = SimulationEngine._mirrored_messages(
            {"ts": "1.0", "channel": "C_X"}, "hello", "0.5",
        )
        assert out == [{"ts": "1.0", "channel": "C_X", "text": "hello", "thread_ts": "0.5"}]

    def test_reported_messages_are_passed_through_unchanged(self):
        posted = [{"ts": "1.0", "channel": "C_X", "text": "a", "thread_ts": None},
                  {"ts": "2.0", "channel": "C_X", "text": "b", "thread_ts": "1.0"}]
        assert SimulationEngine._mirrored_messages(
            {"ts": "1.0", "posted_messages": posted}, "a b", None,
        ) == posted

    # --- and what the engine does with them -------------------------------------

    async def test_a_split_post_becomes_one_row_per_real_message(self):
        """Recording a single row for a post the backend turned into three left two of
        them in Slack with no row at all, named the row's `slack_ts` after the *tail*,
        and made the next restart's Slack reconcile ingest the unrecorded head chunks as
        brand-new inbound messages.
        """
        t = _SplittingTransport(chunks_per_post=3)
        engine = self._engine(t)
        await engine._post_message("su", "general", "a very long post")

        rows = list(engine.message_log._entries)
        assert len(rows) == 3, [r.content for r in rows]
        assert [r.ts for r in rows] == [r.slack_ts for r in rows], (
            "a mirrored row must record the backend's ts as its canonical id"
        )
        # Each row carries the text of its own message, not the whole post three times.
        assert [r.content for r in rows] == [
            "a very long post#0", "a very long post#1", "a very long post#2",
        ]
        # One logical post stays ONE top-level post: the continuations hang off chunk 0,
        # so nobody else's Phase 2 scan sees three roots where one post was written.
        assert rows[0].thread_ts is None
        assert [r.thread_ts for r in rows[1:]] == [rows[0].ts, rows[0].ts]
        assert [r.slack_thread_ts for r in rows[1:]] == [rows[0].ts, rows[0].ts]
        assert len({r.ts for r in rows}) == 3, "two rows share a canonical id"

    async def test_an_unsplit_post_becomes_exactly_one_row(self):
        """Control: an engine that always wrote three rows would pass the test above."""
        engine = self._engine(_SplittingTransport(chunks_per_post=1))
        await engine._post_message("su", "general", "one message")
        rows = list(engine.message_log._entries)
        assert len(rows) == 1
        assert rows[0].content == "one message#0"
        assert rows[0].thread_ts is None

    async def test_every_chunk_of_a_split_reply_keeps_the_callers_thread(self):
        """Control for the root case: a reply's continuations belong to the thread the
        caller named, not to a sub-thread on the first chunk."""
        t = _SplittingTransport(chunks_per_post=3)
        engine = self._engine(t)
        from src.agent.message_log import LogEntry
        engine.message_log.append(LogEntry(
            ts="1700000000.000000", channel="general", sender_agent_id="su",
            sender_name="SuBot", content="root", posted_at=1700000000.0, is_bot=True,
            slack_ts="1700000000.000000",
        ))

        await engine._post_message("su", "general", "long reply",
                                   thread_ts="1700000000.000000")

        replies = [e for e in engine.message_log._entries if e.content.startswith("long reply")]
        assert len(replies) == 3
        assert {r.thread_ts for r in replies} == {"1700000000.000000"}
        assert {r.slack_thread_ts for r in replies} == {"1700000000.000000"}

    async def test_an_unmirrored_post_still_records_exactly_one_row(self):
        """Slack-off: nothing was posted, so the engine mints one canonical id. Iterating
        an empty report must not skip the row entirely."""
        engine = SimulationEngine(agents=[], slack_clients={})
        await engine._post_message("su", "general", "written with slack off")
        rows = list(engine.message_log._entries)
        assert len(rows) == 1
        assert rows[0].slack_ts is None and rows[0].slack_channel_id is None
        assert rows[0].content == "written with slack off"
        assert float(rows[0].ts) > 0
