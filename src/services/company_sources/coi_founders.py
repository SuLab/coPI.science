"""Founder claims from the PI's own competing-interest statements (spec §7.5 Step 1, O11).

A statement counts for the PI only when a clause's SUBJECT names the PI, unlike
`industry_sources/pubmed_coi.py` (spec F13), which credits a whole statement to the PI
and so filed co-authors' ties ("T.M. is a cofounder and holds equity in IMVAQ
Therapeutics", PMID 34290408) under Bert Vogelstein. The claims become manager-confirmed
suggestions, so precision comes first: a missed claim is acceptable, a co-author's
company credited to the PI is not, and when a clause's subject is not clearly the PI
no one is credited. Steps, per record:

1. Locate the PI in the author list: folded surname equal (`company_sources.surname_keys`),
   the first initial equal, and the spelled-out given names agreeing.
2. Build the PI's initials letter-strings from that author's own `Initials` plus the
   surname initial ("VE" + "V" -> "VEV", and first initial + surname initial "VV"); the
   written forms VEV, V.E.V., V. E. V., VV and V.V. all reduce to those strings. Skip the
   record when the PI matches twice or any other author reduces to one of the same
   strings (PMID 37552989: Blair C and Bettegowda C are both "CB", which is why that
   statement spells out "C. Bettegowda"). When another author shares the PI's surname,
   only the initials forms name the PI ("X.J. Wang" is not Jing Wang). A full-name form
   needs a word boundary before the surname ("Jing Zhu" is not Jing Hu).
3. Split the statement into sentences with the protected-abbreviation split (re-implemented
   from `pubmed_coi._sentences`, not imported), and each sentence into segments at ",",
   ";", ":", ".", "and", "while", "whereas" and "but", never inside an initials run. A
   protected period after a lower-case word ("hepatitis B.") still splits when a person
   form and a verb follow it. Each segment then does one of three things:
   - starts a clause: an eLife entry (the segment opens with 2-4 capitals and a
     capitalised word: "SS Co-Founder of", "KK Scientific co-founder of"), or a person
     right before its first usable relationship verb. The subject is that person plus
     the person-only segments joined to it by ",", "and" or ", and" ("A.L., S.C., and
     V.E.V. are"); a list that continues an earlier object list ("royalties from Merck,
     Pfizer, A.L., and B.V. is") keeps only its last person. A segment that opens with
     its verb takes as subject the person segment before it, past degree segments
     ("Chetan Bettegowda, MD, is"). A person is an initials form, "C. Bettegowda", a full
     name of at most three words, or Dr./Prof./Professor and a surname.
   - ends the open clause without starting one: text that is not a person stands before
     its verb ("and his wife is", ", who is", "Luis A. Diaz Jr. is"). What follows is
     credited to no one.
   - continues the open clause: no verb, or a verb with nothing before it.
   Text before a subject is allowed only in a sentence's first segment and only as a
   declaration header (Declaration of interests, Competing interests, Conflict(s) of
   interest, Disclosure(s), "The authors declare ...", never ending in a preposition), or
   after "... report(s)/declare(s)/state(s) that" ("The spouse of V.E.V. is" and "With
   the exception of V.E.V., A.L. ... are" name no V.E.V. subject).
4. Extract the companies after "founder(s) of", "co-founder(s) of", "cofounder(s) of", a
   founder noun coordinated with a title ("co-founder, director and shareholder of",
   "founder & CEO of"), or the verb "co-founded"/"founded" (not "... by", and only right
   after the subject or "has"/"and": "the newly founded Acme Bio" is no claim), up to the
   next clause boundary. A negated phrase (not, never, neither, nor, no, without; "no
   longer" is former, not negated), a "respectively" clause, an alternative ("founders of
   or consultants to") and a title conjunct ("... and CEO of Beta") yield nothing. Each
   name is cut at its first lower-case word that is not a connector ("Delfi Diagnostics
   outside the submitted work" -> "Delfi Diagnostics"), and the list stops at a name that
   is a dotted initials form or an author's name ("DELFI Diagnostics and A.L.").
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
_PLACEHOLDER = ""  # private-use code point; never occurs in PubMed text


def sentences(text: str) -> list[str]:
    """Split on "." or ";" followed by whitespace, except after a single capital letter
    or a listed abbreviation, so "V.E.V. is" and "Paratek Pharmaceuticals, Inc. All"
    stay whole."""
    protected = _PROTECT.sub(lambda m: m.group(1) + _PLACEHOLDER, text or "")
    parts = re.split(r"(?<=[.;])\s+", protected)
    return [p.replace(_PLACEHOLDER, ".").strip() for p in parts if p.strip()]


# --- segments ---
# A "." delimits only when `_protected_period` says it ends neither an initial nor an
# honorific; it is matched without its whitespace so a following " and " still matches.
_DELIM = re.compile(
    r"\s*[,;:]\s*(?:(?:and|while|whereas|but)\s+)?|\.(?=\s)|\s+(?:and|while|whereas|but)\s+"
)
# Delimiters that join the members of one person list.
_LIST_DELIMS = frozenset({",", "and", ", and"})


def _protected_period(text: str, i: int) -> bool:
    before = text[:i]
    return bool(
        re.search(r"(?:^|[^A-Za-z])[A-Z]$", before)
        or re.search(r"(?:^|[^A-Za-z])(?:Dr|Prof|Mr|Mrs|Ms|Jr|Sr|St)$", before)
    )


@dataclass(frozen=True)
class _Segment:
    start: int
    end: int
    delim: str  # the normalised delimiter before this segment; "" for the first
    delim_start: int


def _new_subject_after(sentence: str, i: int) -> bool:
    """Whether the protected period at `i` still ends a clause: it closes a single capital
    after a lower-case word ("hepatitis B.") and a person form and a verb follow it."""
    return bool(
        _LOWER_WORD_INITIAL.search(sentence[:i]) and _PERSON_THEN_VERB.match(sentence, i + 1)
    )


def _segments(sentence: str) -> list[_Segment]:
    out: list[_Segment] = []
    pos, delim, delim_start = 0, "", 0
    for m in _DELIM.finditer(sentence):
        if (
            m.group().startswith(".")
            and _protected_period(sentence, m.start())
            and not _new_subject_after(sentence, m.start())
        ):
            continue
        out.append(_Segment(pos, m.start(), delim, delim_start))
        pos, delim, delim_start = m.end(), " ".join(m.group().split()), m.start()
    out.append(_Segment(pos, len(sentence), delim, delim_start))
    return out


# --- relationship phrase ---
# Lower-case verbs: COI statements write their verbs in lower case, and a capitalised
# word ("IS Pharma") is a name. eLife blocks are verbless ("BV Founder of Thrive Earlier
# Detection"), so their capitalised heads also end a subject.
_REL_VERB = (
    r"\b(?:is|are|was|were|has|have|had|holds?|held|owns?|owned|serves?|served|"
    r"receives?|received|reports?|reported|declares?|declared|consults?|consulted|"
    r"advises?|advised|sits?|sat|chairs?|chaired|leads?|led|remains?|remained|"
    r"co-?founded|founded|divested|sold|became|becomes|acts?|acted|may)\b"
)
_ELIFE_HEAD = (
    r"\b(?:Co-?\s?founders?|Cofounders?|Founders?|Consultants?|Advisors?|Advisers?|"
    r"Owns?|Holds?|Has|Have|Receives?|Received|Serves?|No competing)\b"
)
_ANCHOR = re.compile(rf"{_REL_VERB}|{_ELIFE_HEAD}")

# --- persons ---
_PARTICLE = r"(?:van|von|de|del|della|der|den|di|da|du|la|le|ter|ten|'t|’t|dos|das|do)"
_NAME_WORD = r"[A-Z][\w'’]*[a-z][\w'’]*(?:-[A-Za-z][\w'’]*)*"
_SURNAME = rf"(?:{_PARTICLE}\s+)*{_NAME_WORD}"
# Spaced ("V. E. V."), dotted ("V.E.V.", "B.V.", "K.L", "E.H.-C.H.") or compact ("VEV").
_INITIALS = r"(?:[A-Z]\.(?: [A-Z]\.){1,5}|(?:[A-Z]\.-?){1,5}[A-Z]\.?|[A-Z]{2,6})"
_INITIAL_SURNAME = rf"(?:[A-Z]\.\s?-?){{1,4}}\s?{_SURNAME}"
# At most two given names and middle initials with or without a period ("Victor E
# Velculescu"); a four-word name is not a person, so it ends a clause unattributed.
_FULL_NAME = rf"{_NAME_WORD}(?:\s+{_NAME_WORD})?(?:\s+[A-Z]\.?)*\s+{_SURNAME}"
_HONORIFIC_NAME = rf"(?:Dr|Prof|Professor|Mr|Mrs|Ms)\.?\s+(?:{_FULL_NAME}|{_INITIAL_SURNAME}|{_SURNAME})"
_PERSON = re.compile(rf"{_HONORIFIC_NAME}|{_INITIAL_SURNAME}|{_FULL_NAME}|{_INITIALS}")
_INITIALS_FORM = re.compile(_INITIALS)
# For `_new_subject_after`: "hepatitis B." (not "Victor E." and not "and B.").
_LOWER_WORD_INITIAL = re.compile(r"(?:^|\s)(?!(?:and|or)\s)[a-z][a-z\-]*\s+[A-Z]$")
_PERSON_THEN_VERB = re.compile(rf"\s+(?:{_PERSON.pattern})\s+(?:also\s+)?{_REL_VERB}")
# An eLife entry: initials, then whatever capitalised word opens the entry.
_ELIFE_ENTRY = re.compile(r"\s*([A-Z]{2,4})\s+(?=[A-Z])")
# Degree segments between a name and its verb ("Chetan Bettegowda, MD, is").
_DEGREE = re.compile(
    r"(?:M\.?D|Ph\.?\s?D|D\.?Phil|MBBS|MBChB|MPH|MSc|DVM|DDS|PharmD|FRCPC?|FACS|Jr|Sr)\.?"
)
# Tokens that make a person-shaped string a company, an institution or boilerplate.
_NOT_NAME = frozenset({
    "the", "inc", "llc", "ltd", "corp", "plc", "gmbh", "company", "companies", "bio",
    "biosciences", "biotech", "therapeutics", "diagnostics", "pharma", "pharmaceuticals",
    "oncology", "detection", "sciences", "science", "genomics", "labs", "laboratories",
    "health", "healthcare", "medical", "medicine", "technologies", "holdings", "capital",
    "management", "ventures", "partners", "group", "university", "institute", "hospital",
    "foundation", "board", "directors", "center", "centre", "systems", "ceo", "cso", "cto",
    "cfo", "coo", "cmo", "competing", "interests", "declaration", "authors",
})
_INTRO = re.compile(
    r"(?:additionally|also|furthermore|moreover|in addition|currently|finally|however|"
    r"similarly|likewise)\b[\s,]*",
    re.IGNORECASE,
)
_ADVERB_TAIL = re.compile(r"(?:\s+(?:also|currently|additionally))+\s*$")
_HONORIFIC_PREFIX = r"(?:dr|prof|professor)\.?"
_NAME_START = r"(?<![\w.'\-])"  # a name pattern never starts inside "x.j." or "xiao-jing"
# The only verbless text allowed before a subject: a declaration header.
_HEADER = re.compile(
    r"(?:declarations?\s+of\s+(?:competing\s+)?interests?|"
    r"competing\s+(?:financial\s+)?interests?(?:\s+statement)?|"
    r"conflicts?\s+of\s+interests?(?:\s+statement)?|"
    r"(?:financial\s+)?disclosures?(?:\s+statement)?|"
    r"the\s+authors?\s+(?:declare|report|disclose)s?\b.*)",
    re.IGNORECASE,
)
_ENDS_IN_PREPOSITION = re.compile(
    r"\b(?:of|to|for|with|from|by|in|on|at|than|except|besides|including|between|among|"
    r"about|like)$",
    re.IGNORECASE,
)
_REPORTS_THAT = re.compile(
    r"\b(?:reports?|reported|declares?|declared|states?|stated|discloses?|disclosed|"
    r"notes?|noted|confirms?|confirmed)\s+that$"
)


def _is_person(text: str) -> bool:
    text = text.strip()
    if not text or not _PERSON.fullmatch(text):
        return False
    return not any(name_key(t) in _NOT_NAME for t in text.split())


def _prefix_ok(prefix: str, first_segment: bool, any_prefix: bool) -> bool:
    """Text allowed before a subject person in its segment: "... report(s) that" ("The
    authors report that V.E.V. is"), or a declaration header opening the sentence
    ("Declaration of interests B.V., ..."). A prefix ending in a preposition ("The spouse
    of", "With the exception of") puts the name in object position."""
    if _REPORTS_THAT.search(prefix):
        return True
    if not (any_prefix and first_segment) or _ENDS_IN_PREPOSITION.search(prefix):
        return False
    return bool(_HEADER.fullmatch(prefix))


def _bare_lead(text: str) -> tuple[int, str]:
    """(offset, text) of `text` without surrounding space, an opening discourse adverb
    ("Additionally,") or a closing "also"/"currently"."""
    body = _ADVERB_TAIL.sub("", text.rstrip())
    stripped = body.lstrip()
    offset = len(body) - len(stripped)
    intro = _INTRO.match(stripped)
    if intro:
        offset += intro.end()
        stripped = stripped[intro.end():]
    return offset, stripped


def _person_at_end(
    sentence: str, start: int, end: int, *, first_segment: bool, any_prefix: bool
) -> tuple[int, int, bool] | None:
    """The person form ending sentence[start:end]: (start, end, has_prefix), or None."""
    offset, stripped = _bare_lead(sentence[start:end])
    offset += start
    for tok in re.finditer(r"\S+", stripped):
        if not _is_person(stripped[tok.start():]):
            continue
        prefix = stripped[:tok.start()].strip()
        if prefix and not _prefix_ok(prefix, first_segment, any_prefix):
            return None
        return offset + tok.start(), offset + len(stripped), bool(prefix)
    return None


def _list_start_ok(sentence: str, segs: list[_Segment], k: int) -> bool:
    """Whether a person list whose first member is segment k can be a subject. A list
    reached by "," or a bare "and" continues whatever list stands before it; when that
    earlier list belongs to a clause with a verb, the persons are that clause's object
    ("royalties from Merck, Pfizer, A.L., and B.V. is")."""
    if segs[k].delim not in (",", "and"):
        return True
    while k > 0:
        k -= 1
        if _ANCHOR.search(sentence, segs[k].start, segs[k].end):
            return False
        if segs[k].delim not in _LIST_DELIMS:
            return True
    return True


@dataclass(frozen=True)
class _Clause:
    subject: str
    members: tuple[str, ...]
    lead: str  # the subject's whole segment text up to the anchor (negation scope)
    predicate: str


@dataclass(frozen=True)
class _Hit:
    """Where a segment starts a clause (`members` set) or only ends the open one
    (`members` empty). `start` is where the previous predicate stops."""

    start: int
    lead_start: int
    anchor: int
    members: tuple[str, ...]


def _break(seg: _Segment) -> _Hit:
    return _Hit(seg.delim_start, seg.start, seg.start, ())


def _subject_list(
    sentence: str, segs: list[_Segment], j: int, head: tuple[int, int, bool]
) -> tuple[int, int, tuple[str, ...], bool]:
    """(clause start, lead start, members, whole list kept) for the list ending in
    segment j's head. When the list cannot start a subject only the head is kept."""
    members = [head]
    k = j
    while not members[0][2] and k > 0 and segs[k].delim in _LIST_DELIMS:
        prev = segs[k - 1]
        hit = _person_at_end(sentence, prev.start, prev.end, first_segment=k == 1, any_prefix=True)
        if hit is None:
            break
        members.insert(0, hit)
        k -= 1
    ok = members[0][2] or _list_start_ok(sentence, segs, k)
    if not ok:
        members, k = members[-1:], j
    first = members[0]
    start = first[0] if (first[2] or k == 0) else segs[k].delim_start
    return start, segs[k].start, tuple(sentence[s:e] for s, e, _ in members), ok


def _clause_hit(
    sentence: str, segs: list[_Segment], j: int, head: tuple[int, int, bool], anchor: int
) -> _Hit:
    start, lead_start, members, _ = _subject_list(sentence, segs, j, head)
    return _Hit(start, lead_start, anchor, members)


def _is_degree(sentence: str, seg: _Segment) -> bool:
    return bool(_DEGREE.fullmatch(sentence[seg.start:seg.end].strip()))


def _verb_first_hit(sentence: str, segs: list[_Segment], j: int, anchor: int) -> _Hit | None:
    """A segment that opens with its verb: the person segment before it (past degree
    segments, all joined by ",") is its subject; with no person there it continues the
    open clause. A person joined any other way, or one inside an object list, makes the
    subject unclear, so the segment only ends the open clause."""
    k = j - 1
    while k >= 0 and segs[k + 1].delim == "," and _is_degree(sentence, segs[k]):
        k -= 1
    if k < 0:
        return None
    prev = segs[k]
    head = _person_at_end(sentence, prev.start, prev.end, first_segment=k == 0, any_prefix=True)
    if head is None:
        return None
    if segs[k + 1].delim != ",":
        return _break(segs[j])
    start, lead_start, members, ok = _subject_list(sentence, segs, k, head)
    return _Hit(start, lead_start, anchor, members) if ok else _break(segs[j])


def _segment_hit(sentence: str, segs: list[_Segment], j: int) -> _Hit | None:
    seg = segs[j]
    entry = _ELIFE_ENTRY.match(sentence, seg.start, seg.end)
    if entry:
        return _clause_hit(sentence, segs, j, (entry.start(1), entry.end(1), False), entry.end())
    anchors = list(_ANCHOR.finditer(sentence, seg.start, seg.end))
    if not anchors:
        return None
    if not _bare_lead(sentence[seg.start:anchors[0].start()])[1]:
        return _verb_first_hit(sentence, segs, j, anchors[0].start())
    for n, anchor in enumerate(anchors):
        head = _person_at_end(sentence, seg.start, anchor.start(), first_segment=j == 0, any_prefix=n == 0)
        if head is not None:
            return _clause_hit(sentence, segs, j, head, anchor.start())
    return _break(seg) if j else None


def _parse(sentence: str) -> list[_Clause]:
    """Clauses in order. A predicate runs from its anchor to the next segment that starts
    or ends a clause; text before the first subject is dropped."""
    segs = _segments(sentence)
    hits = [hit for j in range(len(segs)) if (hit := _segment_hit(sentence, segs, j))]
    out: list[_Clause] = []
    for i, hit in enumerate(hits):
        if not hit.members:
            continue
        end = hits[i + 1].start if i + 1 < len(hits) else len(sentence)
        end = max(end, hit.anchor)
        out.append(_Clause(
            subject=sentence[hit.start:hit.anchor], members=hit.members,
            lead=sentence[hit.lead_start:hit.anchor], predicate=sentence[hit.anchor:end],
        ))
    return out


# --- extraction ---
_TITLE = (
    r"(?:a\s+|an\s+|the\s+)?(?:(?:executive|non-executive|scientific|managing)\s+)?"
    r"(?:director|ceo|cso|cto|cmo|coo|cfo|president|chair\w*|chief|"
    r"head|board\s+member|member|advisor|adviser|consultant|officer)\b"
)
# The noun form needs "of" or a coordinated title ("co-founder and director of X",
# "co-founder, director and shareholder of X", "founder & CEO of X"); "T.M. is a
# cofounder and holds equity in IMVAQ" names no object of "founder", and "founder or CEO
# of X" says neither.
_FOUNDER = re.compile(
    rf"\b(?:(?P<co>co-?\s?founders?)|founders?)(?:\s+of\b|(?=(?:\s*[,&]\s*|\s+and\s+){_TITLE}))"
    r"|\b(?P<co2>co-?founded)\b(?!\s+by\b)|\b(?P<verb>founded)\b(?!\s+by\b)",
    re.IGNORECASE,
)
# The verb "founded"/"co-founded" counts only right after the subject, "has" or "and":
# "the newly founded Acme Bio" is an adjective.
_VERB_POSITION = re.compile(r"(?:^|\band\s+)(?:(?:has|have|had)\s+)?(?:also\s+)?$")
# "founders of or consultants to", "founder of, or advisor to", "and/or": an alternative,
# which says nothing about the founder role. An "or" inside a later conjunct ("founders
# of, hold or may hold equity in, and serve ... to manaT Bio", PMID 34290408) is not.
_DISJUNCT_START = re.compile(r"\s*,?\s*(?:and/)?or\b")
_DISJUNCT = re.compile(r",\s*(?:and/)?or\b|\band/or\b")
# "founders of and own equity in ManaT Bio" / "founders of, hold ... equity in, and serve
# as consultants to manaT Bio" / "co-founded and serves on the board of DELFI": when the
# phrase is followed by a coordination, the object is the first name (a token with a
# capital or a digit) after a following "in", "to" or "of".
_COORDINATED = re.compile(r"^(?:,|&|and\b)")
_OBJECT_AFTER = re.compile(r"\b(?:in|to|of)\b[\s,]+")
_WORD = re.compile(r"[A-Za-z][\w'’\-]*")
_TITLE_WORDS = frozenset({
    "ceo", "cso", "cto", "cmo", "coo", "cfo", "cbo", "president", "vice", "chair", "chairman",
    "chairwoman", "chairperson", "director", "directors", "chief", "head", "member", "members",
    "board", "advisory", "scientific", "advisor", "adviser", "consultant", "officer",
    "executive", "managing", "founder", "cofounder", "professor",
})
_CORPORATE_SUFFIX = frozenset({"inc", "llc", "ltd", "corp", "co", "plc", "gmbh", "ag", "sa", "nv", "bv", "llp", "lp"})
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
# Commas are handled by `_object_names` (a serial list continues past them).
_HARD_BOUNDARY = re.compile(
    r"[;:()\[\]]|\.(?=\s|$)|\s+(?:which|that|who|whose|where|with|since|until|from|as)\b"
    rf"|\s+in\s+(?:\d{{4}}|{_MONTHS})\b"
)
# "no longer" is former wording (`_FORMER`), not a negation.
_NEGATION = re.compile(
    r"\b(?:not|never|neither|nor|without)\b|\bno\b(?!\s+longer\b)|n['’]t\b", re.IGNORECASE
)
_RESPECTIVELY = re.compile(r"\brespectively\b", re.IGNORECASE)
_FORMER = re.compile(r"\b(?:former(?:ly)?|previously|divested|sold|no longer)\b", re.IGNORECASE)


def _plausible_name(part: str) -> bool:
    tokens = part.split()
    if not tokens or len(part) > 200 or len(tokens) > 8 or len(part) < 2:
        return False
    head = tokens[0]
    return bool(re.search(r"[A-Z]", head) or (re.search(r"\d", head) and re.search(r"[A-Za-z]", head)))


# Lower-case words a company name may contain; any other lower-case word ends it.
_CONNECTORS = frozenset({"of", "&", "and", "for", "the"}) | PARTICLES


@dataclass(frozen=True)
class _People:
    """Every author's initials strings and surname keys: a company name that is one of
    them is a person standing in an object list, and ends it."""

    letters: frozenset[str]
    surnames: frozenset[str]


def _record_people(authors: list[dict]) -> _People:
    letters: set[str] = set()
    surnames: set[str] = set()
    for author in authors:
        letters |= _author_letter_forms(author)
        surnames |= _author_surname_keys(author)
    return _People(frozenset(letters), frozenset(surnames))


def _names_a_person(name: str, people: _People) -> bool:
    plain = strip_accents(name)
    if _INITIALS_FORM.fullmatch(plain):
        return "." in plain or _letters(plain) in people.letters
    tokens = name.split()
    return any(name_key(" ".join(tokens[i:])) in people.surnames for i in range(len(tokens)))


def _trim_name(name: str) -> str:
    """The name up to its first token with no capital or digit that is not a connector
    ("Delfi Diagnostics outside the submitted work" -> "Delfi Diagnostics")."""
    tokens = name.split()
    keep = tokens[:1]
    for tok in tokens[1:]:
        if not (re.search(r"[A-Z0-9]", tok) or fold(tok) in _CONNECTORS):
            break
        keep.append(tok)
    while len(keep) > 1 and fold(keep[-1]) in _CONNECTORS:
        keep.pop()
    return " ".join(keep)


def _clean(part: str) -> str:
    part = part.strip().strip("\"'“”‘’").strip()
    part = re.sub(r"^the\s+", "", part)
    return part.rstrip(" .").strip()


def _is_title(part: str) -> bool:
    tokens = part.split()
    if tokens and tokens[0].lower() in ("a", "an", "the"):
        tokens = tokens[1:]
    return bool(tokens) and name_key(tokens[0]) in _TITLE_WORDS


def _bare_name(part: str) -> bool:
    tokens = part.split()
    return (
        0 < len(tokens) <= 6
        and all(re.search(r"[A-Z0-9]", t) or t == "&" for t in tokens)
        and not _is_title(part)
    )


def _serial_tail(pieces: list[str]) -> list[str]:
    """The names after the first comma of "X, Y, and Z" / "X, Y and Z": every piece a
    bare name and the list closed by "and"; otherwise none ("X, an Exact Sciences
    Company", "X, serves on the Board")."""
    tail: list[str] = []
    for piece in pieces:
        parts = [p for p in re.split(r"^\s*and\s+|\s+and\s+", piece) if p.strip()]
        if not parts or not all(_bare_name(p) for p in parts):
            return []
        tail.extend(parts)
        if re.search(r"(?:^|\s)and\s", piece):
            return tail
    return []


def _object_names(rest: str, people: _People) -> list[str]:
    span = _HARD_BOUNDARY.split(rest, maxsplit=1)[0]
    pieces = span.split(",")
    names: list[str] = []
    for item in [pieces[0], *_serial_tail(pieces[1:])]:
        for raw in re.split(r"\s+and\s+", item):
            name = _clean(raw)
            if _is_title(name):  # "Acme Bio and CEO of Beta Inc"
                return names
            name = _trim_name(name)
            if _names_a_person(name, people):  # "DELFI Diagnostics and A.L., S.C. and R.B.S. are"
                return names
            if _plausible_name(name) and name_key(name) not in _CORPORATE_SUFFIX:
                names.append(name)
    return names


def _coordinated_object(rest: str) -> int | None:
    """Offset in `rest` of the object of a coordinated founder phrase, or None when a
    capitalised non-title word stands before it (another company: "co-founded and serves
    on the board of DELFI Diagnostics, and owns equity in Exact Sciences" must not reach
    Exact Sciences)."""
    for o in _OBJECT_AFTER.finditer(rest):
        after = rest[o.end():].split(maxsplit=1)
        token = after[0] if after else ""
        if not re.search(r"[A-Z0-9]", token) or name_key(token) in _TITLE_WORDS:
            continue
        between = _WORD.findall(rest[:o.start()])
        if any(w[0].isupper() and name_key(w) not in _TITLE_WORDS for w in between):
            return None
        return o.end()
    return None


def _founder_object(predicate: str, m: re.Match[str]) -> str | None:
    """The text after a founder phrase where its object starts, or None when the phrase
    is an adjective, an alternative, or coordinated with no reachable object."""
    if (m.group("co2") or m.group("verb")) and not _VERB_POSITION.search(predicate, 0, m.start()):
        return None
    rest = predicate[m.end():]
    if _DISJUNCT_START.match(rest):
        return None
    if not _COORDINATED.match(rest.lstrip()):
        return rest
    obj = _coordinated_object(rest)
    if obj is None or _DISJUNCT.search(rest, 0, obj):
        return None
    return rest[obj:]


def _companies_after(lead: str, predicate: str, people: _People) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _FOUNDER.finditer(predicate):
        if _NEGATION.search(lead + predicate[:m.start()]):
            continue
        rest = _founder_object(predicate, m)
        if rest is None:
            continue
        role = "co_founder" if (m.group("co") or m.group("co2")) else "founder"
        out.extend((name, role) for name in _object_names(rest, people))
    return out


# --- the PI ---


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
    the full-name forms are built from. `surname_shared` is set when another author
    has the PI's surname; then only the initials forms name the PI."""

    letters: frozenset[str]
    first_names: tuple[str, ...]
    first_initial: str
    surname: str
    surname_shared: bool = False


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


def _author_surname_keys(author: dict) -> frozenset[str]:
    if author.get("collective"):
        return frozenset()
    return surname_keys(_author_last(author)) | surname_keys(author.get("last") or "")


def _author_given(author: dict) -> str:
    fore_tokens = (author.get("fore") or "").split()
    if fore_tokens and fold(fore_tokens[-1]) in PARTICLES:
        fore_tokens = fore_tokens[:-1]
    return fore_tokens[0] if fore_tokens else (author.get("initials") or "")[:1]


def _is_pi(author: dict, pi: PiName) -> bool:
    if author.get("collective") or not (author.get("last") or "").strip():
        return False
    if not (_author_surname_keys(author) & pi.surname_keys):
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
    others = [a for i, a in enumerate(authors) if i != matches[0]]
    if any(_author_letter_forms(other) & letters for other in others):
        return None
    mine = _author_surname_keys(me)
    given = _author_given(me)
    firsts = tuple(dict.fromkeys(f for f in (fold(pi.first), fold(given)) if len(name_key(f)) > 1))
    return PiForms(
        letters=frozenset(letters),
        first_names=firsts,
        first_initial=name_key(pi.first)[:1],
        surname=_author_last(me),
        surname_shared=any(_author_surname_keys(other) & mine for other in others),
    )


def _surname_regex(surname: str) -> str:
    chunks = [re.escape(c) for c in re.split(r"[^0-9a-z]+", fold(surname)) if c]
    return r"[\s'\-]*".join(chunks)


def _member_names_pi(member: str, forms: PiForms) -> bool:
    """Whether one subject-list member is the PI: an initials form whose letters are the
    PI's, or (unless another author shares the surname) the full name, the initial and
    surname, or Dr./Professor and surname, each ending the member. Middle initials are
    whole tokens and the surname starts a word, so "Jing Zhu", "J. Zhu" and "John Cross"
    do not end in "Hu" or "Ross"."""
    member = member.strip()
    if _INITIALS_FORM.fullmatch(strip_accents(member)):
        return _letters(member) in forms.letters
    if forms.surname_shared:
        return False
    sur = _surname_regex(forms.surname)
    if not sur:
        return False
    text = fold(member)
    middle = r"(?:[a-z]\.\s*|[a-z]\s+){0,2}"
    sur = rf"(?<![a-z0-9]){sur}"
    patterns = [rf"{_NAME_START}{re.escape(f)}\s+{middle}{sur}\Z" for f in forms.first_names]
    patterns.append(rf"{_NAME_START}{re.escape(forms.first_initial)}\.\s*{middle}{sur}\Z")
    patterns.append(rf"{_NAME_START}{_HONORIFIC_PREFIX}\s+{sur}\Z")
    return any(re.search(p, text) for p in patterns)


def founder_claims(record: dict, pi: PiName) -> list[FounderClaim]:
    """Every founder claim this record's competing-interest statement makes for the PI."""
    statement = (record.get("coi_statement") or "").strip()
    if not statement:
        return []
    forms = locate_pi(record, pi)
    if forms is None:
        return []
    people = _record_people([a for a in (record.get("authors") or []) if isinstance(a, dict)])
    pmid = str(record.get("pmid") or "")
    year = record.get("year") if isinstance(record.get("year"), int) else None
    claims: list[FounderClaim] = []
    seen: set[str] = set()
    for sentence in sentences(statement):
        former = bool(_FORMER.search(sentence))
        for clause in _parse(sentence):
            if not any(_member_names_pi(m, forms) for m in clause.members):
                continue
            if _RESPECTIVELY.search(clause.subject + clause.predicate):
                continue
            for name, role in _companies_after(clause.lead, clause.predicate, people):
                key = name_key(name)
                if key in seen:
                    continue
                seen.add(key)
                claims.append(FounderClaim(name, role, pmid, year, sentence, former))
    return claims
