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


# First-person authorship phrasing. Deliberately verb-anchored: "our lab
# co-authored", "we published", "our recent paper". Mentions of *other*
# people's work ("your paper", "the Su lab published") do not match.
_ADVERB = r"(?:recently|actually|just|also|previously|proudly)\s+"
_VERBS = r"(?:co-?authored|co-?wrote|authored|published|wrote)"
_PAPER_NOUN = r"(?:paper|publication|preprint|article|manuscript)"

_FIRST_PERSON_CLAIM_RE = re.compile(
    "|".join(
        [
            rf"\b(?:our|my)\s+labs?(?:['’]s)?\s+(?:{_ADVERB})?{_VERBS}\b",
            rf"\b(?:our|my)\s+(?:{_ADVERB})?{_VERBS}\b",
            rf"\bwe\s+(?:{_ADVERB})?{_VERBS}\b",
            rf"\bI\s+(?:{_ADVERB})?{_VERBS}\b",
            rf"\b(?:our|my)\s+(?:lab(?:['’]s)?\s+)?(?:recent\s+|new\s+|latest\s+|joint\s+)?{_PAPER_NOUN}\b",
        ]
    ),
    re.IGNORECASE,
)

_COAUTHOR_STEM_RE = re.compile(r"\bco-?author(?:ed|ship|s|ing)?\b", re.IGNORECASE)


def makes_first_person_authorship_claim(text: str | None) -> bool:
    """True if ``text`` asserts, in the first person, authorship of a paper."""
    if not text:
        return False
    return bool(_FIRST_PERSON_CLAIM_RE.search(text))


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
