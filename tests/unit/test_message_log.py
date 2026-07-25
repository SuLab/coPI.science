"""Tests for MessageLog and thread participation rules."""

import pytest

from src.agent.message_log import LogEntry, MessageLog


@pytest.fixture
def log():
    ml = MessageLog()
    ml.set_bot_name_map({
        "subot": "su",
        "wisemanbot": "wiseman",
        "cravattbot": "cravatt",
        "grotjahnbot": "grotjahn",
        "kenbot": "ken",
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
