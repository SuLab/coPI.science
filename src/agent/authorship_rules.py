"""Pure-function validators grounding authorship claims in publication records.

GitHub issue #29: an agent publicly claimed first-person co-authorship of a
paper its PI is not an author on. The claim originated with ANOTHER agent
(whose lab genuinely is an author), was confirmed sycophantically, persisted
through working-memory synthesis for six weeks, and finally re-emitted as a
:newspaper: post. These validators run on the emit paths (phase 4 + phase 5 +
_post_message) and on memory synthesis.

Design rules:
- FAIL CLOSED: a lab with no publication records cannot verify any claim, so
  every first-person authorship claim from it is rejected.
- A co-authorship claim that tags other labs is checked against the TAGGED
  labs' records too — the issue-#29 origin message cited a DOI genuinely in
  the sender's own record set; the fabricated part was the claimed co-author.
- Deterministic and conservative: a false rejection costs one regenerated
  draft; a false pass costs a fabricated credential in a real PI's name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.agent.agent import _extract_dois


@dataclass
class LabPublicationRecord:
    """A lab's ground-truth publication state (DB rows ∪ profile DOIs)."""

    dois: set[str] = field(default_factory=set)
    has_records: bool = False


@dataclass
class AuthorshipVerdict:
    ok: bool
    reason: str | None = None


# --- text normalization -----------------------------------------------------
# Slack renders *bold*/_italic_/~strike~ by wrapping (or splitting) words; the
# 2026-08-11 audit's pinned incident text wrapped the verb itself
# ("*recently co-authored*"), which pushed a "*" inside what the claim grammar
# expects to be a word boundary. Unicode hyphens (co‐authored, U+2010) and
# apostrophes (we’ve, U+2019) similarly dodge "co-?" and "'ve". Both are folded
# away before any claim matching. Normalized text is used for DETECTION only —
# DOI *values* are still extracted from the raw text where it matters, since a
# DOI may legitimately contain "_".

_UNICODE_FOLD = str.maketrans({
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark (Slack's apostrophe)
    "ʼ": "'",  # modifier letter apostrophe
})

_EMPHASIS_RUN_RE = re.compile(r"[*~_]+")


def normalize_claim_text(text: str) -> str:
    """Fold unicode hyphen/apostrophe variants; strip Slack emphasis runs."""
    return _EMPHASIS_RUN_RE.sub("", text.translate(_UNICODE_FOLD))


# --- first-person authorship grammar -----------------------------------------
# Runs against normalize_claim_text() output. Subjects are first-person only —
# "we", "I", "ours", "our/my lab|labs|team|group" — so mentions of *other*
# people's work ("your paper", "the Su lab published") do not match. Widened
# per the 2026-08-11 audit (finding C1): auxiliaries ("has co-authored"),
# contractions ("we've"), noun forms ("we're co-authors on", "as a co-author
# of", "I was senior author on"), "behind the ... paper", "contribution to the
# ... paper", "a paper of ours", and dash/paren inserts between subject and
# verb ("Our labs — ours and the Good lab's — co-authored").
_ADVERB = r"(?:recently|actually|just|also|previously|proudly|jointly|together)\s+"
_AUX = r"(?:have|has|had|are|were|is|was|am|been|'ve|'re|'m|'d)\s+"
_VERBS = r"(?:co-?authored|co-?wrote|co-?published|authored|published|wrote)"
_VERBS_INF = r"(?:co-?author|co-?write|author|publish|write)"
_PAPER_NOUN = r"(?:paper|publication|preprint|article|manuscript)"
_FP_GROUP = r"(?:labs?|team|group)"
_AUTHOR_QUAL = r"(?:senior|first|last|corresponding|lead)"
# A parenthetical / dash- or comma-delimited insert between subject and verb.
# Bounded and delimiter-anchored so free-running prose ("our lab admires what
# the Su lab published") cannot bridge the gap.
_INSERT = r"(?:—[^—.!?\n]{0,80}—\s*|\([^()!?\n]{0,80}\)\s*|,[^,.!?\n]{0,80},\s*)?"

_FIRST_PERSON_CLAIM_RE = re.compile(
    "|".join(
        [
            # "our lab (has) (recently) co-authored", "our team published",
            # "Our labs — ours and the Good lab's — actually co-authored"
            rf"\b(?:our|my)\s+{_FP_GROUP}(?:'s)?\s+{_INSERT}(?:{_AUX})*(?:{_ADVERB})*{_VERBS}\b",
            # "our co-authored ..." / "my published ..." (possessive + verb)
            rf"\b(?:our|my)\s+(?:{_ADVERB})?{_VERBS}\b",
            # "we (have) (just) published", "we've co-authored", "I wrote"
            rf"\b(?:we|I|ours)\b\s*(?:{_AUX})*(?:{_ADVERB})*{_VERBS}\b",
            # "we're co-authors on", "I am a co-author of"
            rf"\b(?:we|I)\b\s*(?:{_AUX})?(?:all\s+|both\s+)?(?:a\s+|the\s+)?co-?authors?\b",
            # "as co-authors of the ... paper", "as a co-author on that"
            rf"\b(?:as|being)\s+(?:a\s+|the\s+)?(?:co-?authors?|{_AUTHOR_QUAL}\s+authors?)\s+(?:on|of)\b",
            # "I was senior author on", "we are corresponding authors"
            rf"\b(?:we|I)\b\s*(?:{_AUX})+(?:a\s+|the\s+)?{_AUTHOR_QUAL}\s+authors?\b",
            # "our lab is behind the ... paper"
            rf"\b(?:we|ours|I|(?:our|my)\s+{_FP_GROUP})\b\s+(?:is|are|was|were|am)\s+behind\s+[^.!?\n]{{0,80}}?{_PAPER_NOUN}\b",
            # "our (lab's) (recent) paper"
            rf"\b(?:our|my)\s+(?:{_FP_GROUP}(?:'s)?\s+)?(?:recent\s+|new\s+|latest\s+|joint\s+)?{_PAPER_NOUN}\b",
            # "a paper of ours"
            rf"\b{_PAPER_NOUN}s?\s+of\s+(?:ours|mine)\b",
            # "it was our privilege to co-author that"
            rf"\b(?:our|my)\s+(?:privilege|honou?r|pleasure)\s+to\s+(?:{_ADVERB})*{_VERBS_INF}\b",
            # "our lab's contribution to the ... paper"
            rf"\b(?:our|my)\s+(?:{_FP_GROUP}(?:'s)?\s+)?contributions?\s+to\s+[^.!?\n]{{0,80}}?{_PAPER_NOUN}\b",
        ]
    ),
    re.IGNORECASE,
)

_COAUTHOR_STEM_RE = re.compile(r"\bco-?author(?:ed|ship|s|ing)?\b", re.IGNORECASE)


def makes_first_person_authorship_claim(text: str | None) -> bool:
    """True if ``text`` asserts, in the first person, authorship of a paper."""
    if not text:
        return False
    return bool(_FIRST_PERSON_CLAIM_RE.search(normalize_claim_text(text)))


def claims_coauthorship(text: str | None) -> bool:
    """True if ``text`` contains a co-authorship stem (any subject)."""
    if not text:
        return False
    return bool(_COAUTHOR_STEM_RE.search(text))


def validate_authorship_claims(
    text: str | None,
    own: LabPublicationRecord,
    tagged: dict[str, LabPublicationRecord] | None = None,
) -> AuthorshipVerdict:
    """Validate every first-person authorship claim in a draft message.

    ``tagged`` maps bot names appearing in the text (e.g. "WuBot") to their
    labs' publication records. When the draft makes a first-person
    CO-authorship claim, every tagged lab is treated as a claimed co-author
    and must also hold the cited DOI(s) — this is what catches the issue-#29
    origin case, where the DOI was genuinely in the sender's own records and
    the fabrication was the claimed co-author.
    """
    if not makes_first_person_authorship_claim(text):
        return AuthorshipVerdict(ok=True)

    claimed_dois = _extract_dois(text)

    if not own.has_records:
        return AuthorshipVerdict(
            ok=False,
            reason=(
                "first-person authorship claim, but this lab has no publication "
                "records to verify against (fail closed)"
            ),
        )
    if not claimed_dois:
        return AuthorshipVerdict(
            ok=False,
            reason=(
                "first-person authorship claim without a DOI — unverifiable "
                "(cite the paper's DOI from your publication list)"
            ),
        )
    unknown = claimed_dois - own.dois
    if unknown:
        return AuthorshipVerdict(
            ok=False,
            reason=(
                "first-person authorship claim for DOI(s) not in this lab's "
                f"publication records: {', '.join(sorted(unknown))}"
            ),
        )

    if tagged and claims_coauthorship(text):
        for bot_name, record in tagged.items():
            if not record.has_records:
                return AuthorshipVerdict(
                    ok=False,
                    reason=(
                        f"co-authorship claimed with @{bot_name}, whose lab has "
                        "no publication records to verify against (fail closed)"
                    ),
                )
            missing = claimed_dois - record.dois
            if missing:
                return AuthorshipVerdict(
                    ok=False,
                    reason=(
                        f"co-authorship claimed with @{bot_name}, but "
                        f"{', '.join(sorted(missing))} is not in that lab's "
                        "publication records"
                    ),
                )

    return AuthorshipVerdict(ok=True)


# Memory-synthesis hygiene. Working-memory notes are compressed tables; the
# issue-#29 poisoned row was the subject-less 'Co-authored "Desiderata"
# paper.' — first-person by default when re-read in a later prompt. A line
# with an authorship verb survives only if it either (a) names another lab as
# the explicit subject immediately before the verb, or (b) cites DOI(s) all
# present in this lab's own records.

_AUTHORSHIP_VERB_LINE_RE = re.compile(
    r"\b(?:co-?authored|co-?wrote|authored together|published together"
    r"|our (?:joint )?paper|joint publication|we (?:published|wrote|authored))\b",
    re.IGNORECASE,
)

# "Wu Lab co-authored", "Wu Lab (@WuBot) co-authored", "Su and Wu Labs
# co-authored" — the lab-name subject must sit directly before the verb
# (an intervening table-cell "|" breaks the match, by design).
# The capitalized-token run is bounded at 6 tokens: the unbounded "*" form
# backtracked quadratically on long capitalized runs (audit finding M1 —
# ~85s at 20k tokens, on the event loop). No real lab-name subject is longer.
_OTHER_LAB_SUBJECT_RE = re.compile(
    r"\b(?!(?:Our|My)\s)[A-Z][\w.'-]*(?:\s+(?:and\s+)?[A-Z][\w.'-]*){0,6}\s+[Ll]abs?\b"
    r"(?:\s*\(@\w+\))?\s+(?:recently\s+)?co-?(?:authored|wrote)",
)


def strip_ungrounded_authorship_lines(
    memory_text: str,
    own: LabPublicationRecord,
) -> tuple[str, list[str]]:
    """Drop memory lines asserting authorship the lab's records can't back.

    Returns ``(cleaned_text, stripped_lines)``. Conservative by construction:
    a stripped true fact costs one lost memory note; a kept false fact is
    re-injected into every future prompt (see issue #29).
    """
    kept: list[str] = []
    stripped: list[str] = []
    for line in memory_text.splitlines():
        if _AUTHORSHIP_VERB_LINE_RE.search(line):
            if _OTHER_LAB_SUBJECT_RE.search(line):
                kept.append(line)
                continue
            line_dois = _extract_dois(line)
            grounded = (
                own.has_records and bool(line_dois) and line_dois <= own.dois
            )
            if not grounded:
                stripped.append(line)
                continue
        kept.append(line)
    return "\n".join(kept), stripped
