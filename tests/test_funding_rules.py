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
