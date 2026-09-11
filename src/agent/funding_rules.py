"""Pure-function validators and summarizers for :moneybag: funding threads.

These helpers implement the funding-thread rules in `specs/agent-system.md`:
atomic spin-off (no announcement-only replies), no acknowledgment-only replies,
self-dedup, and structured thread-activity summaries for late joiners.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.agent.foa_pattern import FOA_NUMBER_RE as _FOA_NUMBER_RE
from src.agent.foa_pattern import extract_foa_number
from src.agent.mentions import BOT_TAG_RE
from src.agent.message_log import LogEntry, MessageLog, is_funding_post

# ---------------------------------------------------------------------------
# Announcement-only detector (atomic spin-off rule)
# ---------------------------------------------------------------------------

# Intent + future-post phrases that indicate the agent is merely announcing
# a forthcoming spin-off post instead of creating it.
#
# The apostrophe class in the two contraction phrases covers every code point
# this text can carry it in: U+0027 apostrophe, U+2019 right
# single quotation mark (Slack's smart-quote autocorrect and most LLM output),
# U+02BC modifier letter apostrophe, U+2018 left single quotation mark (an LLM
# that opens a quote and never closes it), U+00B4 acute accent (a common
# keyboard-layout substitution) and U+FF07 fullwidth apostrophe (CJK input
# methods). The classes in the two phrases must stay identical — every code
# point is pinned against both in tests/unit/test_funding_rules.py's
# TestApostropheClass, which fails if one class drifts from the other.
_ANNOUNCEMENT_PHRASES = [
    r"\bi['’ʼ‘´＇]?ll (start|post|create|put up|open|spin ?up|spin ?off|draft|kick off)\b",
    r"\bi will (start|post|create|put up|open|spin ?up|spin ?off|draft|kick off)\b",
    r"\bi['’ʼ‘´＇]?m (going|about) to (start|post|create|put up|open|spin ?up|spin ?off)\b",
    r"\bgoing up now\b",
    r"\bposting (it |the )?(now|shortly|next)\b",
    r"\blook (out )?for (my|the|it)\b",
    r"\bwatch for (my|the|it)\b",
    r"\bsee you (in|over) (the|that) (new|dedicated|spin.?off)\b",
    r"\bspin(?:ning)? (this |it )?off\b(?!.*\b(aim|aims|contribute|bring|model|dataset|assay)\b)",
    r"\b(dedicated|new) (:moneybag: |)?(thread|post) (now|shortly|going up|incoming)\b",
    r"\bthread wrapped\b",
    r"\bthread closed\b",
    r"\bclosing (this |)thread (out|now)\b",
    r"\bmoving to (the |a )?(new|dedicated)\b",
]

_ANNOUNCEMENT_RE = re.compile("|".join(_ANNOUNCEMENT_PHRASES), re.IGNORECASE)

# If the message contains substantive content markers, it's not announcement-only
# even if it happens to also contain a forward-looking phrase.
_SUBSTANTIVE_MARKERS_RE = re.compile(
    r"\b(aim|aims|specific aim|contribute|contribution|dataset|reagent|assay|"
    r"model system|mouse model|cell line|compound|platform|pipeline|screen|"
    r"pathway|target|mechanism|chemistry|proteomic|genomic|structural|"
    r"review criteria|milestone|budget|preliminary data|first experiment)\b",
    re.IGNORECASE,
)


def is_announcement_only_funding_reply(text: str) -> bool:
    """Return True if a funding-thread reply is merely announcing a spin-off.

    Only call this when the target is known to be a funding thread. The
    decision is gated on two checks: (a) a forward-looking announcement
    phrase appears, and (b) no substantive-content marker appears. This
    keeps the filter from catching replies that *also* contain a real
    contribution.
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if not _ANNOUNCEMENT_RE.search(stripped):
        return False
    # If the message has real content alongside the announcement, let it through.
    if _SUBSTANTIVE_MARKERS_RE.search(stripped):
        return False
    # Short messages with an announcement phrase and no substance → reject.
    return True


# ---------------------------------------------------------------------------
# Acknowledgment-only detector
# ---------------------------------------------------------------------------

_ACK_PHRASES = [
    r"^thanks?\b",
    r"^thank you\b",
    r"^sounds good\b",
    r"^great\b",
    r"^agreed\b",
    r"^ack(nowledged)?\b",
    r"^noted\b",
    r"^see you\b",
    r"^will do\b",
    r"^got it\b",
    r"^confirmed\b",
    r"^\+1\b",
    r"^:thumbsup:",
    r"^:\+1:",
]
_ACK_RE = re.compile("|".join(_ACK_PHRASES), re.IGNORECASE)

# A message this long is presumed substantive even if it opens with an ack
# phrase — see is_acknowledgment_only_funding_reply.
# 10, not a rounder-looking 12: a real ack-phrase message ("Agreed, we can
# send the plasmids and the mice next week.") is 11 words, and a threshold
# that does not flip that sentence does not close this gap. Verified safe
# against every TestAcknowledgmentOnly.test_positive_cases fixture — the
# longest ("Sounds good — see you there.") is 6 words.
_ACK_SUBSTANTIVE_WORD_COUNT = 10


def _strip_for_ack_check(text: str) -> str:
    """Strip markdown/emoji/whitespace to the first substantive token."""
    s = text.strip()
    # Drop leading emoji markers and common markdown
    s = re.sub(r"^[\s>*_`~\-]+", "", s)
    s = re.sub(r"^:[a-z_+\-]+:\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^@\w+[,:\-\s]*", "", s)
    return s


def is_acknowledgment_only_funding_reply(text: str) -> bool:
    """Return True if a funding-thread reply is a purely social acknowledgment.

    Checks that (a) the message is short, (b) starts with an ack phrase,
    (c) does not reference an FOA number, :moneybag:, or a substantive
    content marker.
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    # Long messages are presumed substantive.
    if len(stripped) > 200:
        return False
    if _FOA_NUMBER_RE.search(stripped):
        return False
    if ":moneybag:" in stripped:
        return False
    if _SUBSTANTIVE_MARKERS_RE.search(stripped):
        return False
    # A question is substantive engagement, not an ack.
    if "?" in stripped:
        return False
    # A reply this long is doing more than acknowledging, even one that opens
    # with an ack phrase and never touches the (necessarily incomplete) marker
    # vocabulary above: without this, "Agreed, we can send the plasmids
    # and the mice next week." would be rejected as ack-only for lacking a listed
    # noun.
    if len(stripped.split()) >= _ACK_SUBSTANTIVE_WORD_COUNT:
        return False
    cleaned = _strip_for_ack_check(stripped)
    if not cleaned:
        # Only emoji / @mention — treat as ack-only.
        return True
    return bool(_ACK_RE.match(cleaned))


# ---------------------------------------------------------------------------
# Thread activity summary (late-joiner awareness + self-dedup)
# ---------------------------------------------------------------------------


@dataclass
class FundingThreadSummary:
    """Structured summary of prior activity in a :moneybag: thread."""

    alignments: list[tuple[str, str]]  # (sender_name, one_line_excerpt)
    pairings_proposed: list[tuple[str, str]]  # (tagger, tagged_bot_name)
    spinoffs: list[tuple[str, str]]  # (spinoff_thread_id, description)

    def is_empty(self) -> bool:
        return not (self.alignments or self.pairings_proposed or self.spinoffs)


def _first_meaningful_line(content: str, limit: int = 160) -> str:
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        # Skip a leading standalone :moneybag: or heading line
        if s in (":moneybag:",):
            continue
        return s[:limit]
    return content.strip()[:limit]


def summarize_funding_thread(
    message_log: MessageLog,
    thread_ts: str,
    viewer_agent_id: str | None = None,
) -> FundingThreadSummary:
    """Walk a funding thread and extract structured activity.

    - `alignments`: each non-root reply becomes an alignment entry with a
      short excerpt. Root post is excluded (it's the GrantBot FOA post).
    - `pairings_proposed`: any reply that tags another @…Bot is recorded as
      a proposed pairing.
    - `spinoffs`: top-level :moneybag: posts in the log that reference the
      same FOA number as the root but are NOT the root itself.
    """
    history = message_log.get_thread_history(thread_ts)
    if not history:
        return FundingThreadSummary([], [], [])
    root = history[0]
    replies = history[1:]

    # Canonical (upper-case) form — see extract_foa_number. The scan below
    # compares against upper-cased bodies, so the two spellings of one FOA
    # number cannot hide a spin-off from each other.
    foa_number = extract_foa_number(root.content)

    alignments: list[tuple[str, str]] = []
    pairings: list[tuple[str, str]] = []
    seen_pairings: set[tuple[str, str]] = set()

    for entry in replies:
        alignments.append((entry.sender_name, _first_meaningful_line(entry.content)))
        for tag_match in BOT_TAG_RE.finditer(entry.content):
            bot_name = tag_match.group(1)
            key = (entry.sender_name, bot_name.lower())
            if key in seen_pairings:
                continue
            seen_pairings.add(key)
            pairings.append((entry.sender_name, bot_name))

    spinoffs: list[tuple[str, str]] = []
    if foa_number:
        # Scan the log for top-level :moneybag: posts referencing this FOA.
        for entry in message_log._entries:  # noqa: SLF001 - intentional read
            if entry.thread_ts is not None:
                continue
            if entry.ts == thread_ts:
                continue
            if not is_funding_post(entry.content):
                continue
            # Case-insensitive: the number was extracted with an IGNORECASE
            # pattern, so a case-sensitive compare here silently dropped
            # spin-offs whose casing differs from the root's — NIH's own
            # permalink lower-cases the number — and an agent that cannot see
            # the existing spin-off would post a duplicate.
            if foa_number not in entry.content.upper():
                continue
            spinoffs.append((entry.ts, _first_meaningful_line(entry.content)))

    return FundingThreadSummary(alignments, pairings, spinoffs)


def format_funding_thread_summary(summary: FundingThreadSummary) -> str:
    """Render a FundingThreadSummary as a compact markdown block for prompts."""
    if summary.is_empty():
        return "(no prior activity in this thread)"
    lines: list[str] = []
    if summary.alignments:
        lines.append("**Prior alignment replies:**")
        for sender, excerpt in summary.alignments:
            lines.append(f"- {sender}: {excerpt}")
    if summary.pairings_proposed:
        lines.append("")
        lines.append("**Pairings proposed (tags):**")
        for tagger, tagged in summary.pairings_proposed:
            lines.append(f"- {tagger} tagged @{tagged}")
    if summary.spinoffs:
        lines.append("")
        lines.append("**Spin-off posts already created for this FOA:**")
        for spin_id, excerpt in summary.spinoffs:
            lines.append(f"- {spin_id}: {excerpt}")
    return "\n".join(lines)


def format_your_prior_messages(entries: list[LogEntry]) -> str:
    """Render the viewer's own prior messages in a thread for the prompt."""
    if not entries:
        return "(none — this would be your first reply)"
    lines = []
    for e in entries:
        excerpt = _first_meaningful_line(e.content, limit=220)
        lines.append(f"- {excerpt}")
    return "\n".join(lines)
