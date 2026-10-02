"""Founder claims from the PI's own competing-interest statements (spec §7.5 Step 1, O11).

A statement counts for the PI only when a sentence's SUBJECT names the PI, unlike
`industry_sources/pubmed_coi.py` (spec F13), which credits a whole statement to the PI
and so filed co-authors' ties ("T.M. is a cofounder and holds equity in IMVAQ
Therapeutics", PMID 34290408) under Bert Vogelstein. Steps, per record:

1. Locate the PI in the author list: folded surname equal (`company_sources.surname_keys`),
   the first initial equal, and the spelled-out given names agreeing.
2. Build the PI's initials letter-strings from that author's own `Initials` plus the
   surname initial ("VE" + "V" -> "VEV", and first initial + surname initial "VV"); the
   written forms VEV, V.E.V., V. E. V., VV and V.V. all reduce to those strings. Skip the
   record when the PI matches twice or any other author reduces to one of the same
   strings (PMID 37552989: Blair C and Bettegowda C are both "CB", which is why that
   statement spells out "C. Bettegowda").
3. Split the statement into sentences with the protected-abbreviation split (re-implemented
   from `pubmed_coi._sentences`, not imported). The subject is the text before the first
   relationship verb; it names the PI by an initials group, the full name, the initial and
   surname ("C. Bettegowda") or "Dr./Professor <surname>". A subject list counts when the
   PI is one of its members.
4. Extract the companies after "founder(s) of", "co-founder(s) of", "cofounder(s) of",
   "co-founded" or "founded" (not "founded by") up to the next clause boundary, within the
   clause the subject governs (a later ", and R.B.S. is ..." clause has its own subject).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.services.company_sources import (
    PARTICLES,
    PiName,
    fold,
    given_names_agree,
    name_key,
    strip_accents,
    surname_keys,
)

# --- sentence split (protected abbreviations; same algorithm as pubmed_coi._sentences) ---
_PROTECT = re.compile(r"\b([A-Z]|Inc|Corp|Ltd|Co|LLC|Dr|Mr|Mrs|Ms|Jr|Sr|St|vs|etc|GmbH|AG|Prof)\.")
_PLACEHOLDER = "\ue000"  # private-use code point; never occurs in PubMed text


def sentences(text: str) -> list[str]:
    """Split on "." or ";" followed by whitespace, except after a single capital letter
    or a listed abbreviation, so "V.E.V. is" and "Paratek Pharmaceuticals, Inc. All"
    stay whole."""
    protected = _PROTECT.sub(lambda m: m.group(1) + _PLACEHOLDER, text or "")
    parts = re.split(r"(?<=[.;])\s+", protected)
    return [p.replace(_PLACEHOLDER, ".").strip() for p in parts if p.strip()]


# --- subject ---
# Lower-case only: COI statements write their verbs in lower case, and a capitalised
# word ("IS Pharma") is a name.
_REL_VERB = re.compile(
    r"\b(?:is|are|was|were|has|have|had|holds?|held|owns?|owned|serves?|served|"
    r"receives?|received|reports?|reported|declares?|declared|consults?|consulted|"
    r"co-?founded|founded|divested|sold|became|becomes|acts?|acted|may)\b"
)
# One written initials form, bounded by non-letters: spaced ("V. E. V."), dotted
# ("V.E.V.", "B.V.", "K.L", "E.H.-C.H.") or compact ("VEV"). The three shapes are kept
# apart so "LLC. B.V." reads as "LLC" and "B.V.", not as one run "LLCBV".
_INITIALS_GROUP = re.compile(
    r"(?<![A-Za-z.])(?:[A-Z]\.(?: [A-Z]\.){1,5}|(?:[A-Z]\.-?){1,5}[A-Z]\.?|[A-Z]{2,6})(?![A-Za-z])"
)
_HONORIFIC_PREFIX = r"(?:dr|prof|professor)\.?"

# --- extraction ---
_FOUNDER = re.compile(
    r"\b(?:(?P<co>co-?\s?founders?)\s+of\b|founders?\s+of\b|(?P<co2>co-?founded)\b|founded\b(?!\s+by\b))",
    re.IGNORECASE,
)
# "founders of and own equity in ManaT Bio" / "founders of, hold equity in, and serve as
# consultants to manaT Bio": when the phrase is followed by a coordination, the object is
# the first name (a token with a capital or a digit) after a following "in" or "to".
_COORDINATED = re.compile(r"^(?:,|and\b|or\b)")
_OBJECT_AFTER = re.compile(r"\b(?:in|to)\b[\s,]+(?=\S*[A-Z0-9])")
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_BOUNDARY = re.compile(
    r"[,;:()\[\]]|\.(?=\s|$)|\s+(?:which|that|who|whose|where|with|since|until|from|as)\b"
    rf"|\s+in\s+(?:\d{{4}}|{_MONTHS})\b"
)
# A later clause with its own person subject: ", and R.B.S. is", "and C. Bettegowda is".
_PERSON = r"(?:(?:Dr|Prof)\.?\s+[A-Z][\w'’\-]+|[A-Z]\.\s?[A-Z][a-z][\w'’\-]+|(?:[A-Z]\.?\s?-?){1,6})"
_SECOND_SUBJECT = re.compile(
    rf"(?:[.,;]\s*(?:and\s+)?|\s+and\s+)(?:{_PERSON},?\s+(?:and\s+)?)+"
    r"(?:is|are|was|were|has|have|had|holds|owns|serves|receives|reports)\b"
)
_FORMER = re.compile(r"\b(?:former(?:ly)?|previously|divested|sold|no longer)\b", re.IGNORECASE)


@dataclass(frozen=True)
class FounderClaim:
    company_name: str
    pi_role: str  # "founder" | "co_founder"
    pmid: str
    year: int | None
    sentence: str
    former: bool


@dataclass(frozen=True)
class PiForms:
    """How one record writes the PI: initials letter-strings plus the name pieces
    the full-name forms are built from."""

    letters: frozenset[str]
    first_names: tuple[str, ...]
    first_initial: str
    surname: str


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z]", "", strip_accents(text).upper())


def _surname_initials(last: str) -> set[str]:
    """Initial variants of an indexed surname: the first letter ("van 't Erve" -> V), the
    last token's ("E"), every token's ("VTE"), and every hyphen/apostrophe part's
    ("Hamacher-Brady" -> "HB", "O'Callaghan" -> "OC")."""
    folded = strip_accents(last).strip()
    if not folded:
        return set()
    tokens = [t for t in folded.split() if any(ch.isalpha() for ch in t)]
    out = {_letters(folded)[:1]}
    if tokens:
        out.add(_letters(tokens[-1])[:1])
        out.add("".join(_letters(t)[:1] for t in tokens))
    parts = [p for p in re.split(r"[\s\-'’]+", folded) if any(ch.isalpha() for ch in p)]
    out.add("".join(_letters(p)[:1] for p in parts))
    return {s for s in out if s}


def _author_initials(author: dict) -> str:
    inits = _letters(author.get("initials") or "")
    if inits:
        return inits
    fore = strip_accents(author.get("fore") or "")
    return "".join(_letters(p)[:1] for p in re.split(r"[\s\-]+", fore) if p)


def _author_letter_forms(author: dict) -> set[str]:
    inits = _author_initials(author)
    if not inits:
        return set()
    out: set[str] = set()
    for s in _surname_initials(_author_last(author)):
        out.add(inits + s)
        out.add(inits[0] + s)
    return out


def _author_last(author: dict) -> str:
    """LastName, with a particle PubMed stranded at the end of ForeName spliced back
    ("Neal" + "Anya J O'" -> "O'Neal")."""
    last = (author.get("last") or "").strip()
    fore_tokens = (author.get("fore") or "").split()
    if last and fore_tokens and fold(fore_tokens[-1]) in PARTICLES:
        particle = fore_tokens[-1]
        joiner = "" if particle.endswith(("'", "’")) else " "
        return f"{particle}{joiner}{last}"
    return last


def _author_given(author: dict) -> str:
    fore_tokens = (author.get("fore") or "").split()
    if fore_tokens and fold(fore_tokens[-1]) in PARTICLES:
        fore_tokens = fore_tokens[:-1]
    return fore_tokens[0] if fore_tokens else (author.get("initials") or "")[:1]


def _is_pi(author: dict, pi: PiName) -> bool:
    if author.get("collective") or not (author.get("last") or "").strip():
        return False
    keys = surname_keys(_author_last(author)) | surname_keys(author.get("last") or "")
    if not (keys & pi.surname_keys):
        return False
    given = _author_given(author)
    return bool(given) and given_names_agree(given, pi.first)


def locate_pi(record: dict, pi: PiName) -> PiForms | None:
    """The PI's forms on this record, or None when no author is the PI, more than one
    is, or another author reduces to one of the PI's initials strings."""
    authors = [a for a in (record.get("authors") or []) if isinstance(a, dict)]
    matches = [i for i, a in enumerate(authors) if _is_pi(a, pi)]
    if len(matches) != 1:
        return None
    me = authors[matches[0]]
    letters = _author_letter_forms(me)
    if not letters:
        return None
    for i, other in enumerate(authors):
        if i != matches[0] and _author_letter_forms(other) & letters:
            return None
    given = _author_given(me)
    firsts = tuple(dict.fromkeys(f for f in (fold(pi.first), fold(given)) if len(name_key(f)) > 1))
    return PiForms(
        letters=frozenset(letters),
        first_names=firsts,
        first_initial=name_key(pi.first)[:1],
        surname=_author_last(me),
    )


def _surname_regex(surname: str) -> str:
    chunks = [re.escape(c) for c in re.split(r"[^0-9a-z]+", fold(surname)) if c]
    return r"[\s'\-]*".join(chunks)


def subject_names_pi(subject: str, forms: PiForms) -> bool:
    """Whether a sentence subject names the PI (an initials group, the full name, the
    initial and surname, or Dr./Professor and surname)."""
    for m in _INITIALS_GROUP.finditer(strip_accents(subject)):
        if _letters(m.group(0)) in forms.letters:
            return True
    text = fold(subject)
    sur = _surname_regex(forms.surname)
    if not sur:
        return False
    middle = r"(?:[a-z]\.?\s*){0,2}"
    patterns = [rf"\b{re.escape(f)}\s+{middle}{sur}\b" for f in forms.first_names]
    patterns.append(rf"\b{re.escape(forms.first_initial)}\.\s*{middle}{sur}\b")
    patterns.append(rf"\b{_HONORIFIC_PREFIX}\s+{sur}\b")
    return any(re.search(p, text) for p in patterns)


def _plausible_name(part: str) -> bool:
    tokens = part.split()
    if not tokens or len(part) > 200 or len(tokens) > 8 or len(part) < 2:
        return False
    head = tokens[0]
    return bool(re.search(r"[A-Z]", head) or (re.search(r"\d", head) and re.search(r"[A-Za-z]", head)))


def _clean(part: str) -> str:
    part = part.strip().strip("\"'“”‘’").strip()
    part = re.sub(r"^the\s+", "", part)
    return part.rstrip(" .").strip()


def _companies_after(clause: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _FOUNDER.finditer(clause):
        role = "co_founder" if (m.group("co") or m.group("co2")) else "founder"
        rest = clause[m.end():].lstrip()
        if _COORDINATED.match(rest):
            obj = _OBJECT_AFTER.search(rest)
            if obj is None:
                continue
            rest = rest[obj.end():]
        span = _BOUNDARY.split(rest, maxsplit=1)[0]
        for raw in re.split(r"\s+and\s+", span):
            name = _clean(raw)
            if _plausible_name(name):
                out.append((name, role))
    return out


def clauses(sentence: str) -> list[tuple[str, str]]:
    """(subject, predicate) pairs: the sentence is cut where a new person subject starts
    after the current clause's verb (", and R.B.S. is ...", or "LLC. B.V., K.W.K., and
    S.Z. are ..." when the split kept "LLC." protected). A piece with no relationship
    verb has no subject and is dropped."""
    out: list[tuple[str, str]] = []
    start = 0
    while start < len(sentence):
        verb = _REL_VERB.search(sentence, start)
        if verb is None:
            break
        nxt = _SECOND_SUBJECT.search(sentence, verb.end())
        end = nxt.start() if nxt else len(sentence)
        out.append((sentence[start:verb.start()], sentence[verb.start():end]))
        if nxt is None:
            break
        start = nxt.start()
    return out


def founder_claims(record: dict, pi: PiName) -> list[FounderClaim]:
    """Every founder claim this record's competing-interest statement makes for the PI."""
    statement = (record.get("coi_statement") or "").strip()
    if not statement:
        return []
    forms = locate_pi(record, pi)
    if forms is None:
        return []
    pmid = str(record.get("pmid") or "")
    year = record.get("year") if isinstance(record.get("year"), int) else None
    claims: list[FounderClaim] = []
    seen: set[str] = set()
    for sentence in sentences(statement):
        former = bool(_FORMER.search(sentence))
        for subject, predicate in clauses(sentence):
            if not subject_names_pi(subject, forms):
                continue
            for name, role in _companies_after(predicate):
                key = name_key(name)
                if key in seen:
                    continue
                seen.add(key)
                claims.append(FounderClaim(name, role, pmid, year, sentence, former))
    return claims
