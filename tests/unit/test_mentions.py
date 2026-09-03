"""BOT_TAG_RE / extract_bot_mentions — the shared bot-tag matcher (issue #20 COR-8)."""

from src.agent.mentions import BOT_TAG_RE, extract_bot_mentions


class TestBotTagReIsFullyCaseInsensitive:
    def test_matches_every_case_variant(self):
        for text in ("@subot", "@SUBOT", "@SuBot", "@SUBot"):
            assert BOT_TAG_RE.search(text) is not None, text

    def test_does_not_match_a_slack_uid_mention(self):
        assert BOT_TAG_RE.search("<@U12345>") is None


class TestExtractBotMentions:
    def test_a_literal_mention_yields_the_lowercased_name(self):
        assert extract_bot_mentions("@SUBOT thanks") == ["subot"]

    def test_a_slack_uid_mention_resolves_through_the_map(self):
        assert extract_bot_mentions("<@U12345>", {"U12345": "su"}) == ["su"]

    def test_an_unresolvable_uid_mention_is_dropped_not_guessed(self):
        # UZZZZZZ, not U_UNKNOWN: a real Slack bot_user_id is alnum-only, and
        # an underscore isn't in the uid group's [A-Za-z0-9]+ class — U_UNKNOWN
        # would fail to match the <@...> alternative at all and pass this
        # assertion for the wrong reason (see red-team M5).
        assert extract_bot_mentions("<@UZZZZZZ>", {"U12345": "su"}) == []
        assert extract_bot_mentions("<@U12345>", None) == []

    def test_a_mid_sentence_uid_mention_is_still_found(self):
        assert extract_bot_mentions("hey <@U12345> look", {"U12345": "su"}) == ["su"]

    def test_first_match_shadowing_is_preserved_for_callers_that_want_it(self):
        # Order of appearance, not dedup — callers that want "first mention
        # only" (message_log._extract_tagged_agent) take mentions[0].
        assert extract_bot_mentions("@GrantBot posted this — @WisemanBot?") == [
            "grantbot", "wisemanbot",
        ]
