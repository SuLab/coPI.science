"""Tests for MessageLog and thread participation rules."""

import pytest

from src.agent.message_log import LogEntry, MessageLog
from src.services.cohorts import SERVICE_AGENT_IDS


@pytest.fixture
def log():
    ml = MessageLog()
    ml.set_bot_name_map({
        "subot": "su",
        "wisemanbot": "wiseman",
        "cravattbot": "cravatt",
        "grotjahnbot": "grotjahn",
        "kenbot": "ken",
        # SimulationEngine's constructor seeds one entry per service bot, keyed
        # by the agent_id (which is also the lowercased bot name), because their
        # inbound posts have to be attributable. Mirrored here so the thread
        # rules below are exercised against the map production installs — an
        # unmapped @GrantBot would resolve to None for the boring reason.
        **{service_id: service_id for service_id in SERVICE_AGENT_IDS},
    })
    return ml


def _post(ts, channel, agent_id, name, content, thread_ts=None):
    return LogEntry(
        ts=ts,
        channel=channel,
        sender_agent_id=agent_id,
        sender_name=name,
        content=content,
        thread_ts=thread_ts,
        posted_at=float(ts),
        is_bot=True,
    )


# ---------------------------------------------------------------
# get_thread_allowed_agents
# ---------------------------------------------------------------

class TestThreadAllowedAgents:
    def test_tagged_post_reserves_thread(self, log):
        log.append(_post("1", "general", "ken", "KenBot", "Hey @GrotjahnBot, check this out"))
        allowed = log.get_thread_allowed_agents("1")
        assert allowed == {"ken", "grotjahn"}

    def test_tagged_post_blocks_third_party(self, log):
        log.append(_post("1", "general", "ken", "KenBot", "Hey @GrotjahnBot, check this"))
        log.append(_post("2", "general", "grotjahn", "GrotjahnBot", "Interesting!", thread_ts="1"))
        allowed = log.get_thread_allowed_agents("1")
        assert allowed == {"ken", "grotjahn"}
        assert "su" not in allowed
        assert "cravatt" not in allowed

    def test_tagged_post_reserved_even_with_no_replies(self, log):
        """A tagged post is reserved before anyone replies."""
        log.append(_post("1", "general", "su", "SuBot", "Idea for @WisemanBot"))
        allowed = log.get_thread_allowed_agents("1")
        assert allowed == {"su", "wiseman"}

    def test_untagged_post_no_replies_is_open(self, log):
        """Untagged post with no replies should be open to anyone."""
        log.append(_post("1", "general", "wiseman", "WisemanBot", "Interesting UPR finding"))
        allowed = log.get_thread_allowed_agents("1")
        assert allowed is None

    def test_untagged_post_one_reply_locks_to_two(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "UPR finding"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "Tell me more", thread_ts="1"))
        allowed = log.get_thread_allowed_agents("1")
        assert allowed == {"wiseman", "cravatt"}

    def test_untagged_post_third_party_blocked(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "UPR finding"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "Tell me more", thread_ts="1"))
        allowed = log.get_thread_allowed_agents("1")
        assert "su" not in allowed

    def test_nonexistent_thread_returns_none(self, log):
        assert log.get_thread_allowed_agents("999") is None

    def test_tag_extraction_case_insensitive(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Hey @wisemanbot, thoughts?"))
        allowed = log.get_thread_allowed_agents("1")
        assert allowed == {"su", "wiseman"}

    def test_unrecognized_tag_treated_as_untagged(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Hey @UnknownBot, thoughts?"))
        allowed = log.get_thread_allowed_agents("1")
        # Unknown tag — no reservation, treated as open
        assert allowed is None


# ---------------------------------------------------------------
# Service-bot tags (_extract_tagged_agent's SERVICE_AGENT_IDS exemption)
#
# A service bot (grantbot) is in the bot-name map because ingestion must
# attribute its :moneybag: posts, but it has no roster slot and never takes a
# turn, so it can never reply. Letting it out of _extract_tagged_agent would
# reserve a non-funding thread for {poster, grantbot} and leave it dead on
# arrival: the only other agent entitled to speak there does not exist.
# ---------------------------------------------------------------

class TestServiceBotTagsDoNotReserveThreads:
    @pytest.mark.parametrize("service_id", sorted(SERVICE_AGENT_IDS))
    def test_a_service_tag_extracts_nothing(self, log, service_id):
        # Control on the same call: the map does resolve the name, so None is
        # the exemption firing and not a lookup miss.
        assert log._bot_name_to_id[service_id] == service_id
        assert log._extract_tagged_agent(f"nice find @{service_id.title()}") is None

    def test_a_non_funding_root_tagging_a_service_bot_stays_open(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Following up on @GrantBot's R01"))
        # NOT {"su", "grantbot"}: the tag branch is skipped, so the generic
        # 2-party rule applies, and with one participant the thread is open.
        assert log.get_thread_allowed_agents("1") is None

    def test_the_thread_still_locks_to_the_first_two_real_participants(self, log):
        """The exemption re-opens the thread; it does not disable the 2-party rule."""
        log.append(_post("1", "general", "su", "SuBot", "Following up on @GrantBot's R01"))
        log.append(_post("2", "general", "wiseman", "WisemanBot", "We'd apply", thread_ts="1"))
        assert log.get_thread_allowed_agents("1") == {"su", "wiseman"}

    def test_a_service_tag_shadows_a_later_roster_tag(self, log):
        """First-match semantics, now scoped to routing.

        _extract_tagged_agent still stops at the first mention: its remaining
        caller routes the message to a single agent. Participation no longer
        does — _extract_tagged_agents filters per mention, so the roster tag
        after the service tag reserves the thread instead of leaving it open to
        everyone. This expectation was updated deliberately, as the previous
        version of this test asked (#20 COR-8): the old None was the *open*
        2-party fallback, i.e. a thread any agent could join.
        """
        log.append(
            _post("1", "general", "su", "SuBot", "@GrantBot posted this — @WisemanBot?")
        )
        assert log._extract_tagged_agent("@GrantBot posted this — @WisemanBot?") is None
        allowed = log.get_thread_allowed_agents("1")
        assert allowed == {"su", "wiseman"}
        assert not (allowed & SERVICE_AGENT_IDS)

    def test_a_roster_tag_still_reserves_the_thread(self, log):
        """Positive control: the exemption must not disarm the tag rule itself."""
        log.append(_post("1", "general", "su", "SuBot", "Thoughts @WisemanBot?"))
        assert log.get_thread_allowed_agents("1") == {"su", "wiseman"}

    def test_a_funding_root_is_open_even_when_it_tags_a_roster_bot(self, log):
        """The :moneybag: check precedes the tag branch, so funding stays open."""
        log.append(
            _post(
                "1", "funding-opportunities", "grantbot", "GrantBot",
                ":moneybag: *New FOA* — RM1, relevant to @WisemanBot",
            )
        )
        assert log.get_thread_allowed_agents("1") is None
        # And the same text without the marker would have locked it, so the
        # assertion above is the funding exemption and not the service one.
        log.append(
            _post(
                "2", "funding-opportunities", "grantbot", "GrantBot",
                "*New FOA* — RM1, relevant to @WisemanBot",
            )
        )
        assert log.get_thread_allowed_agents("2") == {"grantbot", "wiseman"}


class TestExtractTaggedAgentResolvesSlackUidMentions:
    def test_a_uid_mention_resolves_to_the_tagged_agent(self, log):
        # Slack real bot_user_ids are alnum-only (e.g. "U0AMQGYBFL7"); an
        # underscore in the fixture uid would silently miss the uid branch of
        # extract_bot_mentions's regex (`[A-Za-z0-9]+`) and pass for the
        # wrong reason (the same trap red-team M5 flags for the sibling
        # mentions.py test — verified by running both forms).
        log.set_bot_uid_map({"UWISEMAN1": "wiseman"})
        assert log._extract_tagged_agent("thoughts <@UWISEMAN1>?") == "wiseman"

    def test_an_unmapped_uid_extracts_nothing(self, log):
        log.set_bot_uid_map({})
        assert log._extract_tagged_agent("thoughts <@UZZZZZZ>?") is None

    def test_an_unknown_bot_name_still_extracts_nothing(self, log):
        # Pre-fix behaviour, preserved (red-team B4): get_thread_allowed_agents
        # locks a thread on this value, so an unrecognised bot name must come
        # back None, not the raw token — a phantom agent_id must never come
        # out of here.
        assert log._extract_tagged_agent("ping @GhostBot") is None


# ---------------------------------------------------------------
# A root that tags more than one bot (#20 COR-8)
# ---------------------------------------------------------------

@pytest.fixture
def log_with_two_bots():
    """A log with BOTH mention maps populated.

    Deliberately not the module `log` fixture: `<@Uxxx>` mentions resolve
    through the uid map, literal `@Tag` mentions through the name map, and a
    fixture that sets only one of them makes every assertion below pass
    vacuously (get_thread_allowed_agents falls through to the 2-party rule and
    returns None for a human root with no replies).
    """
    ml = MessageLog()
    ml.set_bot_name_map({
        "subot": "su",
        "wisemanbot": "wiseman",
        "cravattbot": "cravatt",
        **{service_id: service_id for service_id in SERVICE_AGENT_IDS},
    })
    ml.set_bot_uid_map({
        "U111": "su",
        "U222": "wiseman",
        "U333": "cravatt",
        **{f"USVC{n}": s for n, s in enumerate(sorted(SERVICE_AGENT_IDS))},
    })
    return ml


def _human_entry(ts, content, channel="general", thread_ts=None):
    """A PI message: sender_agent_id is None and is_bot is False."""
    return LogEntry(
        ts=ts,
        channel=channel,
        sender_agent_id=None,
        sender_name="Dr. PI",
        content=content,
        thread_ts=thread_ts,
        posted_at=float(ts),
        is_bot=False,
    )


class TestMultiTagRootParticipation:
    """#20 COR-8 asked for tag ROUTING. Locking participation to the *first*
    translated uid regressed the case the issue exists to fix, and opening the
    thread instead would break specs/agent-system.md:278-287 ("No third agent
    may join"). The correct set is the poster plus every tagged agent."""

    def test_a_human_root_tagging_two_bots_allows_exactly_those_two(self, log_with_two_bots):
        log = log_with_two_bots
        log.append(_human_entry("100.1", "Hey <@U111> and <@U222>, compare notes"))
        assert log.get_thread_allowed_agents("100.1") == {"su", "wiseman"}

    def test_a_third_agent_is_still_excluded(self, log_with_two_bots):
        log = log_with_two_bots
        log.append(_human_entry("100.2", "<@U111> <@U222> go"))
        allowed = log.get_thread_allowed_agents("100.2")
        assert allowed is not None, "a tagged thread must stay reserved, not open to the roster"
        assert "cravatt" not in allowed

    def test_a_single_tag_root_is_unchanged(self, log_with_two_bots):
        log = log_with_two_bots
        log.append(_human_entry("100.3", "<@U111> thoughts?"))
        assert log.get_thread_allowed_agents("100.3") == {"su"}

    def test_a_bot_root_tagging_two_bots_keeps_the_poster_too(self, log_with_two_bots):
        log = log_with_two_bots
        log.append(
            _post("100.4", "general", "cravatt", "CravattBot",
                  "@SuBot @WisemanBot — either of you seen this?")
        )
        assert log.get_thread_allowed_agents("100.4") == {"cravatt", "su", "wiseman"}

    def test_a_service_tag_beside_a_roster_tag_reserves_only_the_roster_bot(
        self, log_with_two_bots,
    ):
        """The per-mention service exemption survives the multi-tag scan.

        grantbot never takes a turn, so it must not consume a thread slot; the
        roster bot the PI tagged in the same breath still gets one.
        """
        log = log_with_two_bots
        svc_uid = sorted(k for k in log._bot_uid_to_agent if k.startswith("USVC"))[0]
        log.append(_human_entry("100.5", f"<@{svc_uid}> found this — <@U222> interested?"))
        allowed = log.get_thread_allowed_agents("100.5")
        assert allowed == {"wiseman"}
        assert not (allowed & SERVICE_AGENT_IDS)

    def test_a_root_tagging_only_service_bots_stays_on_the_two_party_rule(
        self, log_with_two_bots,
    ):
        """Control for the test above: with nothing left after the exemption the
        tag branch must not fire at all."""
        log = log_with_two_bots
        svc_uid = sorted(k for k in log._bot_uid_to_agent if k.startswith("USVC"))[0]
        log.append(_human_entry("100.6", f"thanks <@{svc_uid}>"))
        assert log.get_thread_allowed_agents("100.6") is None


# ---------------------------------------------------------------
# get_new_top_level_posts
# ---------------------------------------------------------------

class TestGetNewTopLevelPosts:
    def test_returns_only_top_level(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Top level"))
        log.append(_post("2", "general", "wiseman", "WisemanBot", "Reply", thread_ts="1"))
        posts = log.get_new_top_level_posts(since=0, channels={"general"}, exclude_agent_id="cravatt")
        assert len(posts) == 1
        assert posts[0].ts == "1"

    def test_excludes_own_posts(self, log):
        log.append(_post("1", "general", "su", "SuBot", "My post"))
        posts = log.get_new_top_level_posts(since=0, channels={"general"}, exclude_agent_id="su")
        assert len(posts) == 0

    def test_filters_by_channel(self, log):
        log.append(_post("1", "general", "su", "SuBot", "In general"))
        log.append(_post("2", "structural-biology", "su", "SuBot", "In structural"))
        posts = log.get_new_top_level_posts(since=0, channels={"general"}, exclude_agent_id="cravatt")
        assert len(posts) == 1
        assert posts[0].channel == "general"

    def test_filters_by_cursor(self, log):
        log.append(_post("1.0", "general", "su", "SuBot", "Old post"))
        log.append(_post("5.0", "general", "wiseman", "WisemanBot", "New post"))
        posts = log.get_new_top_level_posts(since=3.0, channels={"general"}, exclude_agent_id="cravatt")
        assert len(posts) == 1
        assert posts[0].ts == "5.0"


# ---------------------------------------------------------------
# get_thread_history
# ---------------------------------------------------------------

class TestGetThreadHistory:
    def test_includes_root_and_replies(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Root"))
        log.append(_post("2", "general", "wiseman", "WisemanBot", "Reply 1", thread_ts="1"))
        log.append(_post("3", "general", "su", "SuBot", "Reply 2", thread_ts="1"))
        history = log.get_thread_history("1")
        assert len(history) == 3
        assert history[0].ts == "1"

    def test_empty_thread(self, log):
        history = log.get_thread_history("999")
        assert history == []

    def test_orders_replies_by_posted_at_not_insertion_order(self, log):
        # A reply ingested late by the DB poller or the Slack reconcile is
        # appended after entries that came *after* it in real time. Insertion
        # order would hand the LLM a scrambled thread.
        log.append(_post("1", "general", "su", "SuBot", "Root"))
        log.append(_post("30", "general", "wiseman", "WisemanBot", "third", thread_ts="1"))
        log.append(_post("20", "general", "su", "SuBot", "second", thread_ts="1"))
        log.append(_post("10", "general", "wiseman", "WisemanBot", "first", thread_ts="1"))

        history = log.get_thread_history("1")
        assert [e.content for e in history] == ["Root", "first", "second", "third"]

    def test_root_stays_first_even_if_a_reply_predates_it(self, log):
        # A writer whose clock runs behind can stamp a reply below the root's
        # posted_at; the root is still the thread's parent.
        log.append(_post("100", "general", "su", "SuBot", "Root"))
        log.append(_post("50", "general", "wiseman", "WisemanBot", "skewed", thread_ts="100"))
        log.append(_post("200", "general", "su", "SuBot", "later", thread_ts="100"))

        history = log.get_thread_history("100")
        assert [e.content for e in history] == ["Root", "skewed", "later"]

    def test_equal_posted_at_keeps_insertion_order(self, log):
        # Stable sort: nothing reshuffles when timestamps tie.
        log.append(_post("1", "general", "su", "SuBot", "Root"))
        for i, content in enumerate(("a", "b", "c")):
            entry = _post(f"{i + 2}", "general", "wiseman", "WisemanBot", content, thread_ts="1")
            entry.posted_at = 5.0
            log.append(entry)

        history = log.get_thread_history("1")
        assert [e.content for e in history] == ["Root", "a", "b", "c"]


# ---------------------------------------------------------------
# get_tags_for_agent
# ---------------------------------------------------------------

class TestGetTagsForAgent:
    def test_finds_tagged_posts(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Hey @WisemanBot check this"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "No tag here"))
        tags = log.get_tags_for_agent("WisemanBot", since=0)
        assert len(tags) == 1
        assert tags[0].ts == "1"

    def test_respects_cursor(self, log):
        log.append(_post("1.0", "general", "su", "SuBot", "Old @WisemanBot tag"))
        log.append(_post("5.0", "general", "cravatt", "CravattBot", "New @WisemanBot tag"))
        tags = log.get_tags_for_agent("WisemanBot", since=3.0)
        assert len(tags) == 1
        assert tags[0].ts == "5.0"


# ---------------------------------------------------------------
# has_new_reply_from_other
# ---------------------------------------------------------------

class TestHasNewReplyFromOther:
    def test_detects_reply(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Root"))
        log.append(_post("2", "general", "wiseman", "WisemanBot", "Reply", thread_ts="1"))
        assert log.has_new_reply_from_other("1", "su", since=0) is True

    def test_ignores_own_reply(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Root"))
        log.append(_post("2", "general", "su", "SuBot", "My own reply", thread_ts="1"))
        assert log.has_new_reply_from_other("1", "su", since=0) is False

    def test_respects_cursor(self, log):
        log.append(_post("1", "general", "su", "SuBot", "Root"))
        log.append(_post("2.0", "general", "wiseman", "WisemanBot", "Old reply", thread_ts="1"))
        assert log.has_new_reply_from_other("1", "su", since=3.0) is False


# ---------------------------------------------------------------
# get_last_bot_sender_in_channel (private-channel turn-taking)
# ---------------------------------------------------------------

class TestLastBotSenderInChannel:
    def test_returns_most_recent_bot(self, log):
        log.append(_post("1", "priv-x", "su", "SuBot", "first"))
        log.append(_post("2", "priv-x", "wiseman", "WisemanBot", "second"))
        log.append(_post("3", "priv-x", "su", "SuBot", "third"))
        assert log.get_last_bot_sender_in_channel("priv-x") == "su"

    def test_scoped_by_channel(self, log):
        log.append(_post("1", "priv-x", "su", "SuBot", "hi"))
        log.append(_post("2", "priv-y", "wiseman", "WisemanBot", "hi"))
        assert log.get_last_bot_sender_in_channel("priv-x") == "su"
        assert log.get_last_bot_sender_in_channel("priv-y") == "wiseman"

    def test_none_when_empty(self, log):
        assert log.get_last_bot_sender_in_channel("priv-x") is None

    def test_skips_human_messages(self, log):
        log.append(_post("1", "priv-x", "su", "SuBot", "bot"))
        # Human message (is_bot=False, no sender_agent_id)
        human = LogEntry(
            ts="2", channel="priv-x", sender_agent_id=None,
            sender_name="PI", content="human msg", posted_at=2.0, is_bot=False,
        )
        log.append(human)
        assert log.get_last_bot_sender_in_channel("priv-x") == "su"


# ---------------------------------------------------------------
# Idempotent append + persist callback (DB-primary store)
# ---------------------------------------------------------------

class TestAppendIdempotencyAndPersist:
    def test_append_returns_true_then_false_on_duplicate_ts(self, log):
        assert log.append(_post("1", "general", "su", "SuBot", "first")) is True
        # Same ts: skipped, returns False, no duplicate stored.
        assert log.append(_post("1", "general", "su", "SuBot", "dup")) is False
        assert len(log) == 1
        # The original content is retained (the duplicate is dropped).
        assert log.get_entry("1").content == "first"

    def test_persist_callback_fires_once_per_new_append(self, log):
        seen = []
        log.set_persist_callback(lambda e: seen.append(e.ts))
        log.append(_post("1", "general", "su", "SuBot", "a"))
        log.append(_post("1", "general", "su", "SuBot", "a-dup"))  # skipped
        log.append(_post("2", "general", "wiseman", "WisemanBot", "b"))
        assert seen == ["1", "2"]

    def test_load_entry_bypasses_callback(self, log):
        seen = []
        log.set_persist_callback(lambda e: seen.append(e.ts))
        log.load_entry(_post("1", "general", "su", "SuBot", "restored"))
        assert len(log) == 1
        assert seen == []  # rebuild path must not re-persist
        # Still idempotent on ts.
        log.load_entry(_post("1", "general", "su", "SuBot", "again"))
        assert len(log) == 1


# ---------------------------------------------------------------
# Insertion order is not time order: the DB inbound poller and the Slack
# reconcile append entries whose posted_at predates what is already stored, so
# every "most recent" query must key on posted_at, not on the tail of _entries.
# ---------------------------------------------------------------

class TestOrderingIsByPostedAtNotInsertion:
    def test_latest_timestamp_is_the_max_not_the_last_appended(self, log):
        log.append(_post("100", "general", "su", "SuBot", "newest"))
        # Ingested afterwards, but older — a tail read would move the cursor back.
        log.append(_post("40", "general", "wiseman", "WisemanBot", "older, late"))
        assert log.latest_timestamp == 100.0

    def test_latest_timestamp_is_zero_on_an_empty_log(self, log):
        assert log.latest_timestamp == 0.0

    def test_latest_timestamp_counts_restored_entries(self, log):
        log.load_entry(_post("70", "general", "su", "SuBot", "restored"))
        assert log.latest_timestamp == 70.0

    def test_agent_posts_slice_keeps_the_newest_by_posted_at(self, log):
        # Newest post first, then two older ones ingested late. With limit=2 an
        # insertion-order slice would drop "newest" — the post the Phase 5 dedup
        # context and the daily cap most need to see.
        log.append(_post("300", "general", "su", "SuBot", "newest"))
        log.append(_post("100", "general", "su", "SuBot", "old-a"))
        log.append(_post("200", "general", "su", "SuBot", "old-b"))
        got = log.get_agent_top_level_posts("su", limit=2)
        assert [e.content for e in got] == ["old-b", "newest"]  # oldest first

    def test_agent_posts_still_exclude_replies_and_other_agents(self, log):
        log.append(_post("10", "general", "su", "SuBot", "root"))
        log.append(_post("20", "general", "su", "SuBot", "reply", thread_ts="10"))
        log.append(_post("30", "general", "wiseman", "WisemanBot", "other"))
        assert [e.content for e in log.get_agent_top_level_posts("su")] == ["root"]

    def test_last_bot_sender_ignores_a_late_appended_older_message(self, log):
        log.append(_post("10", "priv-x", "wiseman", "WisemanBot", "first"))
        log.append(_post("20", "priv-x", "su", "SuBot", "second — the real latest"))
        # Reconcile pulls in a message that predates both; scanning the log
        # backwards would name wiseman the last poster and hand su another turn.
        log.append(_post("5", "priv-x", "wiseman", "WisemanBot", "older, late"))
        assert log.get_last_bot_sender_in_channel("priv-x") == "su"

    def test_last_bot_sender_breaks_posted_at_ties_by_insertion(self, log):
        log.append(_post("10", "priv-x", "wiseman", "WisemanBot", "a"))
        tie = _post("10", "priv-x", "su", "SuBot", "b")
        tie.ts = "10-b"  # distinct id, identical posted_at
        log.append(tie)
        assert log.get_last_bot_sender_in_channel("priv-x") == "su"


class TestPurgeThread:
    def _log_with_thread(self):
        from src.agent.message_log import LogEntry, MessageLog

        log = MessageLog()
        root = LogEntry(
            ts="1.0", channel="general", sender_agent_id="grantbot", sender_name="GrantBot",
            content="root", posted_at=1.0, is_bot=True,
        )
        reply = LogEntry(
            ts="2.0", channel="general", sender_agent_id="su", sender_name="SuBot",
            content="a reply", thread_ts="1.0", posted_at=2.0, is_bot=True,
        )
        unrelated = LogEntry(
            ts="3.0", channel="general", sender_agent_id="wu", sender_name="WuBot",
            content="unrelated root", posted_at=3.0, is_bot=True,
        )
        log.append(root)
        log.append(reply)
        log.append(unrelated)
        return log

    def test_purge_removes_the_root_and_every_reply(self):
        log = self._log_with_thread()

        removed = log.purge_thread("1.0")

        assert removed == 2
        assert log.get_entry("1.0") is None
        assert log.get_entry("2.0") is None
        assert log.get_entry("3.0") is not None
        assert [e.ts for e in log._entries] == ["3.0"]

    def test_purge_of_an_unknown_thread_is_a_noop(self):
        log = self._log_with_thread()

        removed = log.purge_thread("9999999999.999999")

        assert removed == 0
        assert len(log._entries) == 3
