"""Tests for funding_rules validators and thread summarizer."""

import pytest

from src.agent.funding_rules import (
    FundingThreadSummary,
    format_funding_thread_summary,
    format_your_prior_messages,
    is_acknowledgment_only_funding_reply,
    is_announcement_only_funding_reply,
    summarize_funding_thread,
)
from src.agent.message_log import LogEntry, MessageLog


def _entry(ts, agent_id, name, content, thread_ts=None, channel="funding-opportunities"):
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


def test_foa_cache_and_funding_rules_share_the_same_compiled_pattern():
    """Regression guard for issue #23 COR-27: both call sites must be backed by the one shared
    pattern, not independently-maintained copies that can re-diverge."""
    from src.agent import foa_cache, funding_rules
    from src.agent.foa_pattern import FOA_NUMBER_RE

    assert foa_cache.FOA_PATTERN is FOA_NUMBER_RE
    assert funding_rules._FOA_NUMBER_RE is FOA_NUMBER_RE


# ---------------------------------------------------------------
# Announcement-only detector
# ---------------------------------------------------------------


class TestAnnouncementOnly:
    @pytest.mark.parametrize("text", [
        "Thanks @PetrascheckBot — I'll start a dedicated :moneybag: thread now.",
        "Spinning this off — watch for my post.",
        "Going up now. See you in the new thread.",
        "Thread wrapped. Moving to the dedicated thread.",
        "Posting it now — look for my post shortly.",
        "Confirmed — I'll post a new :moneybag: thread tagging you.",
        # COR-28a: curly (U+2019) and modifier-letter (U+02BC) apostrophes must
        # be caught exactly like the ASCII form — LLM output and Slack's own
        # smart-quote autocorrect routinely produce the curly form.
        "I’ll spin up a dedicated thread.",
        "Iʼll spin up a dedicated thread.",
        "I’m going to start a new thread.",
    ])
    def test_positive_cases(self, text):
        assert is_announcement_only_funding_reply(text) is True

    @pytest.mark.parametrize("text", [
        # Substantive replies — must not trip
        "Our APPswe/PSEN1dE9 mice and TargetSeeker-MS platform directly address "
        "the FOA's preclinical target validation milestones. Specific Aim 1 could "
        "focus on compound triage in C. elegans followed by mouse validation.",
        "Strong alignment with PAR-25-297. We contribute autophagy activator AA-20 "
        "and our APPswe/PSEN1dE9 mouse model for in vivo validation.",
        # Question-driven reply
        "What review criteria matter most for this U01 — are preliminary data "
        "on target engagement required at submission?",
        # Empty
        "",
        "   ",
    ])
    def test_negative_cases(self, text):
        assert is_announcement_only_funding_reply(text) is False

    def test_mixed_announcement_with_substance_allowed(self):
        # Has announcement phrase but also substantive content → allowed.
        text = (
            "I'll start with Aim 1: ISR/HRI activators tested in your "
            "APPswe/PSEN1dE9 mice. TargetSeeker-MS for target engagement "
            "validation."
        )
        assert is_announcement_only_funding_reply(text) is False


# ---------------------------------------------------------------
# Acknowledgment-only detector
# ---------------------------------------------------------------


class TestAcknowledgmentOnly:
    @pytest.mark.parametrize("text", [
        "Thanks!",
        "Sounds good — see you there.",
        "Agreed.",
        "Will do.",
        "Confirmed.",
        "Got it, thanks.",
        "@WisemanBot sounds good",
        ":thumbsup:",
    ])
    def test_positive_cases(self, text):
        assert is_acknowledgment_only_funding_reply(text) is True

    @pytest.mark.parametrize("text", [
        "Agreed — on PAR-25-297, we can contribute APPswe/PSEN1dE9 mice and "
        "TargetSeeker-MS for target engagement validation.",
        ":moneybag: PAR-25-297 — aligning on Aim 1.",
        "Thanks — one question: does the FOA allow subcontracts to international labs?",
        "Our specific aim would be autophagy activator AA-20 tested in APPswe mice.",
    ])
    def test_negative_cases(self, text):
        assert is_acknowledgment_only_funding_reply(text) is False

    @pytest.mark.parametrize("text", [
        # COR-28b's literal reproduction from findings/issue_23.md:30 — 11 words,
        # so a 12-word threshold does not fix this at all; 10 does.
        "Agreed, we can send the plasmids and the mice next week.",
        # the general failure mode: vocabulary the fixed marker list misses,
        # demonstrated with a materially longer, equally vocabulary-avoiding sentence.
        "Agreed, we can package up the mice and the plasmids and ship them "
        "to your team early next week.",
    ])
    def test_a_substantive_reply_starting_with_an_ack_word_is_not_rejected(self, text):
        # COR-28b: opens with "Agreed" (an ack phrase) and contains none of
        # _SUBSTANTIVE_MARKERS_RE's fixed vocabulary, but is unambiguously a
        # real logistics commitment, not a bare acknowledgment.
        assert is_acknowledgment_only_funding_reply(text) is False

    def test_long_pure_pleasantry_is_an_accepted_false_negative(self):
        # Accepted cost of the >= 10-word threshold (issue #23 COR-28b): a
        # pleasantry with no substantive content still clears the word-count
        # cutoff meant to rescue substantive replies, so it is (wrongly)
        # treated as not-ack-only. Pinning this rather than "fixing" it keeps
        # the fix from re-introducing the original bug (rejecting the
        # 11-word "Agreed, we can send the plasmids and the mice next
        # week." reply) — see the word-count threshold's own comment.
        text = "Thanks so much for the tag, really looking forward to working together."
        assert len(text.split()) >= 10
        assert is_acknowledgment_only_funding_reply(text) is False


# ---------------------------------------------------------------
# Thread summarizer
# ---------------------------------------------------------------


@pytest.fixture
def log_with_funding_thread():
    ml = MessageLog()
    ml.set_bot_name_map({
        "wisemanbot": "wiseman",
        "petrascheckbot": "petrascheck",
        "forlibot": "forli",
    })
    # Root: GrantBot funding post
    ml.append(_entry(
        "100", None, "GrantBot",
        ":moneybag: *Funding Opportunity*\nPAR-25-297 Alzheimer's Drug-Development Program",
    ))
    # Wiseman replies, tags Petrascheck
    ml.append(_entry(
        "101", "wiseman", "WisemanBot",
        ":moneybag: PAR-25-297 — our ISR/HRI activators align with the FOA. "
        "@PetrascheckBot your aging models could complement ours.",
        thread_ts="100",
    ))
    # Petrascheck replies
    ml.append(_entry(
        "102", "petrascheck", "PetrascheckBot",
        ":moneybag: PAR-25-297 — strong alignment. We bring APPswe/PSEN1dE9 mice "
        "and TargetSeeker-MS for target validation.",
        thread_ts="100",
    ))
    # A spin-off post referencing the same FOA — top-level
    ml.append(_entry(
        "200", "wiseman", "WisemanBot",
        ":moneybag: PAR-25-297 — Wiseman/Petrascheck joint aims draft. "
        "@PetrascheckBot let's develop specific aims here.",
    ))
    return ml


class TestSummarizer:
    def test_collects_alignment_replies(self, log_with_funding_thread):
        summary = summarize_funding_thread(log_with_funding_thread, "100")
        assert len(summary.alignments) == 2
        senders = [s for s, _ in summary.alignments]
        assert "WisemanBot" in senders
        assert "PetrascheckBot" in senders

    def test_collects_pairings(self, log_with_funding_thread):
        summary = summarize_funding_thread(log_with_funding_thread, "100")
        assert any(
            tagger == "WisemanBot" and tagged.lower() == "petrascheckbot"
            for tagger, tagged in summary.pairings_proposed
        )

    def test_detects_spinoff(self, log_with_funding_thread):
        summary = summarize_funding_thread(log_with_funding_thread, "100")
        assert len(summary.spinoffs) == 1
        assert summary.spinoffs[0][0] == "200"

    def test_empty_thread(self):
        ml = MessageLog()
        summary = summarize_funding_thread(ml, "nonexistent")
        assert summary.is_empty()

    def test_format_summary_renders_sections(self, log_with_funding_thread):
        summary = summarize_funding_thread(log_with_funding_thread, "100")
        rendered = format_funding_thread_summary(summary)
        assert "Prior alignment replies" in rendered
        assert "Pairings proposed" in rendered
        assert "Spin-off posts" in rendered
        assert "PAR-25-297" in rendered

    def test_format_empty(self):
        empty = FundingThreadSummary([], [], [])
        assert "no prior activity" in format_funding_thread_summary(empty).lower()


class TestTagRegexCaseInsensitivity:
    """COR-28c: @-mentions of a bot tag must resolve regardless of case — Slack's own autocomplete
    and manual typing both routinely produce @GRANTBOT, @SuBOT, etc."""

    @pytest.mark.parametrize("mention,name", [
        ("@grantbot", "grantbot"),
        ("@GRANTBOT", "GRANTBOT"),
        ("@SuBot", "SuBot"),
        ("@SuBOT", "SuBOT"),
    ])
    def test_pairing_detection_is_case_insensitive(self, mention, name):
        ml = MessageLog()
        ml.set_bot_name_map({"wisemanbot": "wiseman"})
        ml.append(_entry("100", None, "GrantBot", ":moneybag: PAR-25-297 opportunity"))
        ml.append(_entry(
            "101", "wiseman", "WisemanBot",
            f"Aligning on this. {mention} take a look.", thread_ts="100",
        ))
        summary = summarize_funding_thread(ml, "100")
        assert summary.pairings_proposed == [("WisemanBot", name)], summary.pairings_proposed


class TestYourPriorMessages:
    def test_empty(self):
        assert "none" in format_your_prior_messages([]).lower()

    def test_renders_entries(self):
        entries = [
            _entry("1", "wiseman", "WisemanBot", "First reply about ISR/HRI.", thread_ts="100"),
            _entry("2", "wiseman", "WisemanBot", "Second reply narrowing aims.", thread_ts="100"),
        ]
        rendered = format_your_prior_messages(entries)
        assert "First reply" in rendered
        assert "Second reply" in rendered
