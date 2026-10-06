"""Deterministic attribution of a competing-interest statement to named authors (spec
2026-10-05 §6.2, P4, D10), shared by company discovery's founder verification
(`company_sources/coi_llm.py`) and the industry COI source
(`industry_sources/pubmed_coi.py`). It imports neither side's job modules: company
discovery may not import the industry modules (tests/unit/test_company_discovery_isolation.py),
and nothing here is industry-specific.

What it holds: the normalised form a quote or a company is matched in (`norm`,
`norm_map`, `accent_free`), sentence spans that survive initials and corporate
abbreviations (`sentence_spans`), how one author is written in a sentence (`pi_regex`,
`Person`, `other_authors`), clause splitting (`clause_splits`), and the run of names that
ends nearest a company (`last_run`). `Statement` puts them together for one statement and
one PI (`coi_founders.locate_pi`'s `PiForms`).

The COI rule is `Statement.credit`: a company is credited to the PI only when the run of
named persons nearest before it, inside its clause, names one of the PI's forms, or
(with `count_all_authors`) says "all/each author(s)". The founder rule built on the same
pieces is `coi_llm._Statement.names_pi`."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from src.services.company_sources.coi_founders import (
    PiForms,
    _author_given,
    _author_last,
    _author_letter_forms,
)
from src.services.person_names import fold, name_key, strip_accents

TYPOGRAPHY = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
    "‹": "<", "›": ">",  # undo `coi_llm._escape` in a quote that echoes the escaped statement
})


def norm_map(text: str) -> tuple[str, list[int]]:
    """`norm(text)` and, for each of its characters, the index in `text` of the
    character it came from, so a match in the normalised form maps back to the
    original. NFKC runs per base character plus its combining marks, so a decomposed
    accent composes the same way on both sides."""
    chars: list[str] = []
    origin: list[int] = []
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        for ch in unicodedata.normalize("NFKC", text[i:j]).translate(TYPOGRAPHY):
            chars.append(ch)
            origin.append(i)
        i = j
    out: list[str] = []
    where: list[int] = []
    gap: int | None = None  # index of the first whitespace character of a pending run
    for ch, at in zip(chars, origin, strict=True):
        if ch.isspace():
            gap = at if gap is None else gap
            continue
        if gap is not None and out:
            out.append(" ")
            where.append(gap)
        gap = None
        out.append(ch)
        where.append(at)
    return "".join(out), where


def norm(text: str) -> str:
    """NFKC, typographic dashes and quotes folded to ASCII, whitespace runs collapsed:
    the form in which a quoted sentence must occur in the statement. The folding only
    forgives a model that retyped a curly quote or a non-breaking hyphen, or echoed an
    angle bracket as `coi_llm._escape` sent it."""
    return norm_map(text)[0]


# --- sentences ------------------------------------------------------------------

#: A candidate sentence end: ".", "!" or "?", any closing quotes or brackets, then
#: whitespace; group 1 is the next word character, which must be a capital or a digit.
#: ";" and ":" never end a sentence here, so a semicolon-chained disclosure ("…
#: personal fees from X; and is a co-founder of Y.") and an eLife block entry stay whole.
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[\"“‘(\[]?(\w))")
#: Words whose period never ends a sentence, compared case-folded: honorifics, "et al.",
#: "vs.", "no." / "nos." (serial and patent numbers), "vol.", "fig.", "approx.", "ca.".
#: Any one-letter word is protected as well: initials ("V.E.V.", "A."), "e.g.", "i.e.",
#: "S.A.".
_ABBREVIATIONS = frozenset({
    "dr", "prof", "mr", "mrs", "ms", "mx", "jr", "sr", "st", "al", "vs",
    "no", "nos", "vol", "fig", "approx", "ca",
})
#: Corporate abbreviations ("A/S" counts too). Their period is part of the name only when
#: another corporate word follows ("Acme Co. Ltd."); before any other capital it ends the
#: sentence ("… Novo Nordisk A/S. V.E.V. is a founder of …"), so one sentence never
#: absorbs the next sentence's subject. A bracketed or quoted group right after the
#: period is looked past first (`_continues_after_corporate`).
_CORPORATE = frozenset({"inc", "ltd", "co", "corp", "llc", "gmbh", "ag"})
_CORPORATE_NEXT = _CORPORATE | {"kg", "sa", "plc", "pty", "limited", "company", "corporation"}
_LAST_WORD = re.compile(r"[^\W\d_]+$")
_NEXT_WORD = re.compile(r"[^\W\d_]+")
#: Opening brackets and quotes, each with its closer.
_OPENERS = {"(": ")", "[": "]", '"': '"', "“": "”", "‘": "’"}


def _continues_after_corporate(text: str, after: int) -> bool:
    """Whether the text from `after` continues the name or sentence that a corporate
    abbreviation's period sits in: another corporate word ("Acme Co. Ltd."), or, when a
    bracketed or quoted group opens there, a lower-case word or punctuation after its
    closer ("Acme Inc. (Baltimore, MD) and owns stock"). An unclosed group, or a capital
    after it, ends the sentence."""
    if after < len(text) and text[after] in _OPENERS:
        close = text.find(_OPENERS[text[after]], after + 1)
        if close < 0:
            return False
        rest = text[close + 1:].lstrip()
        if rest[:1] and (rest[0] in ",;:.!?)]" or rest[0].islower()):
            return True  # a later stop, if any, is the sentence end
        after = len(text) - len(rest)
    nxt = _NEXT_WORD.match(text, after)
    return nxt is not None and nxt.group().casefold() in _CORPORATE_NEXT


def _protected_period(text: str, at: int, after: int) -> bool:
    """Whether the "." at `at` closes an initial or an abbreviation that does not end
    the sentence; `after` is where the next sentence would start."""
    word = _LAST_WORD.search(text, max(0, at - 12), at)
    if word is None:
        return False
    w = word.group().casefold()
    slash = word.start() > 0 and text[word.start() - 1] == "/"  # the "S" of "A/S"
    if w in _ABBREVIATIONS or (len(w) == 1 and not slash):
        return True
    if w in _CORPORATE or slash:
        return _continues_after_corporate(text, after)
    return False


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of each sentence of `text`, whitespace trimmed. A sentence
    ends at "." "!" or "?" (plus closing quotes or brackets) followed by whitespace and
    a capital or a digit, unless the "." closes an initial, a listed abbreviation or a
    corporate abbreviation that continues the name (`_protected_period`)."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        nxt = m.group(1)
        if not (nxt.isupper() or nxt.isdigit()):
            continue
        if text[m.start()] == "." and _protected_period(text, m.start(), m.end()):
            continue
        spans.append(_trimmed(text, start, m.start() + len(m.group().rstrip())))
        start = m.end()
    spans.append(_trimmed(text, start, len(text)))
    return [(s, e) for s, e in spans if s < e]


# --- names ------------------------------------------------------------------------

_HONORIFIC = r"(?:Drs|Dr|Profs|Prof|Professors|Professor|Mr|Mrs|Ms|Mx)\.?\s"


def initials_pattern(letters: str) -> str:
    """One initials string in any written form ("VEV", "V.E.V.", "V. E. V.", "V.E.V"),
    never as part of a longer run: no letter or period before it, no word character,
    ".X" or " X." after it ("V.E.V.S." and "V. E. V. S." are someone else).

    A two-letter string written without a period ("CA", "MD") is too often a state or a
    degree: it counts only at the start of the text or right after ", " or "; ", and
    only when a space and a word follow ("CA is a founder", "…, BV Founder of"), never
    in "(San Diego, CA)". Its dotted forms ("C.A.", "C. A.") count anywhere."""
    head = r"(?<![\w.])(?<![A-Z]\.\s)"
    tail = r"(?!\w)(?!\.[^\W\d_])(?!\.?\s[A-Z]\.)"
    if len(letters) == 2:
        a, b = (re.escape(ch) for ch in letters)
        dotted = rf"{head}{a}\.\s?{b}\.?{tail}"
        bare = rf"(?:^|(?<=, )|(?<=; )){a}{b}(?=\s\w)(?!\s[A-Z]\.)"
        return rf"(?:{dotted}|{bare})"
    body = r"\.?\s?".join(re.escape(ch) for ch in letters)
    return rf"{head}{body}\.?{tail}"


def name_patterns(forms: PiForms) -> list[str]:
    """The surname forms (case as indexed, or with a capital first letter) after an
    honorific, an initial or one of the given names ("Dr Velculescu", "V. Velculescu",
    "Victor E. Velculescu"). A surname of four or more letters also counts alone, in the
    group named `bare` ("Velculescu"); a shorter one ("He", "Li") never does, so a
    pronoun or a common word is never the person. A `bare` match that is one word of a
    longer capitalised name is discarded by `Person.mentions`."""
    surname = strip_accents(forms.surname).strip()
    if not surname:
        return []
    variants = sorted({surname, surname[:1].upper() + surname[1:]})
    tail = "(?:" + "|".join(re.escape(v) for v in variants) + r")(?!\w)"
    prefixes = [_HONORIFIC, r"[A-Z]\.\s?"] + [
        re.escape(strip_accents(f).title()) + r"\s(?:[A-Z]\.?\s)?" for f in forms.first_names
    ]
    patterns = [rf"(?<!\w){p}{tail}" for p in prefixes]
    if len(name_key(surname)) >= 4:
        patterns.append(rf"(?P<bare>(?<!\w){tail})")
    return patterns


def pi_regex(forms: PiForms, *, names: bool) -> re.Pattern[str]:
    """What names one person in a text: every initials form, plus the surname forms
    when `names` is set. Run on `strip_accents(norm(sentence))` or `accent_free(norm(sentence))`;
    case-sensitive."""
    patterns = [initials_pattern(s) for s in sorted(forms.letters, key=lambda x: (-len(x), x))]
    if names:
        patterns += name_patterns(forms)
    return re.compile("|".join(patterns) or r"(?!)")


#: A capitalised word ending right before a bare surname, by a space or a hyphen
#: ("Johns Hopkins", "Bristol-Myers"); and a hyphen joining one right after it.
_CAPITALISED_BEFORE = re.compile(r"(?<![\w'’])[A-Z][\w'’]*[ -]$")
_CAPITALISED_AFTER = re.compile(r"-[A-Z]")
_RELATIVES = (
    r"(?:spouses?|wife|wives|husbands?|partners?|sons?|daughters?|child|children|family|"
    r"relatives?|fathers?|mothers?|brothers?|sisters?|parents?|siblings?)(?!\w)"
)
#: A possessive and a relative noun right after a mention, one lower-case word allowed
#: between ("Dr. Lee's spouse", "G.L.'s immediate family"): a relative of the person, not
#: the person. Any other possessive ("G.L.'s company, Acme Inc") still names the person.
_RELATIVE_OF = re.compile(r"['’]s\s+(?:[a-z-]+\s+)?" + _RELATIVES)
#: A relative noun and "of" ending right before a mention ("the spouse of G.L."), searched
#: in the `_RELATIVE_WINDOW` characters before it; the same meaning as `_RELATIVE_OF`.
_RELATIVE_BEFORE = re.compile(r"(?<![\w-])" + _RELATIVES + r"\s+of\s+$", re.IGNORECASE)
_RELATIVE_WINDOW = 24


def _a_relative(text: str, start: int, end: int) -> bool:
    """Whether the mention text[start:end] names a relative of the person, not the person
    (`_RELATIVE_OF` after it, `_RELATIVE_BEFORE` before it)."""
    window = text[max(0, start - _RELATIVE_WINDOW):start]
    return bool(_RELATIVE_OF.match(text, end) or _RELATIVE_BEFORE.search(window))


@dataclass(frozen=True)
class Person:
    """How one author of the record is named in a sentence (`pi_regex`)."""

    pattern: re.Pattern[str]

    def mentions(self, text: str) -> list[tuple[int, int]]:
        """(start, end) of each mention in `text`. A bare surname (`name_patterns`)
        right after a capitalised word or hyphen-joined to one is part of another name
        ("Johns Hopkins" is not Dr Hopkins, "Bristol-Myers" is not Dr Myers) and is
        skipped; a given name or honorific before it is matched as a prefixed form."""
        out = []
        for m in self.pattern.finditer(text):
            if m.groupdict().get("bare") is not None and (
                _CAPITALISED_BEFORE.search(text[: m.start()]) or _CAPITALISED_AFTER.match(text, m.end())
            ):
                continue
            out.append(m.span())
        return out


def other_authors(authors: list[dict], forms: PiForms) -> tuple[Person, ...]:
    """Every author but the PI (`forms.position`), named by their initials forms and
    their surname forms. With `position` 0 the PI is not excluded, so every PI mention
    also reads as another author's and nothing verifies."""
    out = []
    for n, author in enumerate(authors, 1):
        if n == forms.position or author.get("collective"):
            continue
        given = _author_given(author)
        other = PiForms(
            letters=frozenset(_author_letter_forms(author)),
            first_names=(fold(given),) if len(name_key(given)) > 1 else (),
            first_initial=name_key(given)[:1],
            surname=_author_last(author),
        )
        out.append(Person(pi_regex(other, names=True)))
    return tuple(out)


def clause_splits(authors: list[dict]) -> re.Pattern[str]:
    """Where a sentence splits into clauses for `Statement.clause`: at ";" and at an
    eLife block start, ", " before an author's undotted initials form of two to four
    letters and a capitalised word ("…, BV Founder of …", "…, SZ Hold equity …")."""
    letters = {
        s for author in authors for s in _author_letter_forms(author) if 2 <= len(s) <= 4
    }
    alts = "|".join(sorted(letters, key=lambda x: (-len(x), x)))
    return re.compile(";" + (rf"|, (?=(?:{alts}) [A-Z])" if alts else ""))


#: Opening words, compared case-folded, that continue the previous clause's subject
#: across a ";" (`_subjectless`). An allowlist on purpose: a clause opening with any other
#: word ("the remaining authors", "co-authors", "funding", "spouse", "Johns Hopkins
#: University") names nobody, since a missed credit costs less than a false one.
#: Verbs and auxiliaries of a disclosure, and the pronouns that refer back.
_CONTINUING_VERBS = frozenset({
    "is", "are", "was", "were", "has", "have", "had", "holds", "hold", "held", "owns", "own",
    "owned", "serves", "serve", "served", "receives", "receive", "received", "reports",
    "report", "reported", "consults", "consult", "consulted", "acts", "act", "acted",
    "advises", "advise", "advised", "works", "work", "worked", "sits", "sit", "sat",
    "chairs", "chair", "chaired", "declares", "declare", "declared", "discloses",
    "disclose", "disclosed", "founded", "co-founded", "cofounded",
    "he", "she", "they",
})
#: Adverbs: they continue only before a word of `_CONTINUING_VERBS` ("currently holds").
_CONTINUING_ADVERBS = frozenset({"currently", "previously", "formerly", "now"})
#: Prepositions: they continue only right before the company ("; also to Beta Inc"), never
#: when they open a phrase of their own ("; for this study, Beta Pharma provided …").
_CONTINUING_PREPOSITIONS = frozenset({"to", "for", "with", "from", "of", "in", "at", "by", "on"})
#: The leading connectives skipped, with any comma, before the opening word is read; the
#: words after them must still pass ("however, the remaining authors …" does not,
#: "In addition, he holds …" does). Case-insensitive.
_CONNECTIVE_RUN = re.compile(
    r"\s*(?:(?:as\s+well\s+as|in\s+addition|additionally|moreover|furthermore|further|"
    r"besides|also|however|and|&|plus|but|while|whereas)(?![\w-])\s*,?\s*)*",
    re.IGNORECASE,
)
#: A bracketed aside, ignored when reading a clause ("Beta Inc (equity)").
_ASIDE = re.compile(r"\([^()]*\)")


def _words(text: str) -> list[str]:
    """The words of `text` with asides, commas and edge punctuation removed."""
    words = (w.strip("\"'()[]:;.!?") for w in _ASIDE.sub(" ", text).replace(",", " ").split())
    return [w for w in words if any(ch.isalnum() for ch in w)]


def _names_only(words: list[str]) -> bool:
    """Whether `words` are capitalised names joined only by "and" (a company list)."""
    return all(w[0].isupper() or w[0].isdigit() or w.casefold() == "and" for w in words)


def _subjectless(head: str, tail: str = "") -> bool:
    """Whether a clause continues the subject of the clause before it. `head` is its text
    before the company (or, for an earlier clause, all of it); `tail` is its text after
    the company. Asides are ignored; a head holding ":" never continues ("; Funding: Beta
    Inc"). After the leading connectives (`_CONNECTIVE_RUN`):

    - no words left, or capitalised names only, is a company list ("; Beta Inc (equity);
      and Gamma Inc"), which continues only when the company is no subject itself: `tail`
      must hold nothing but further names ("; Beta Inc funded this study" and "; Novartis
      and Pfizer had no role" name nobody);
    - otherwise a comma left in the head stops it ("; in 2019, Beta Inc acquired …"), and
      the opening word must be a verb or pronoun (`_CONTINUING_VERBS`), an adverb before
      one (`_CONTINUING_ADVERBS`), or a preposition right before the company
      (`_CONTINUING_PREPOSITIONS`) whose clause passes the `tail` test as a list does."""
    head = _ASIDE.sub(" ", head)
    if ":" in head:
        return False
    rest = head[_CONNECTIVE_RUN.match(head).end():]
    words = _words(rest)
    if _names_only(words):
        return _names_only(_words(tail))
    if "," in rest:
        return False
    first = words[0].casefold()
    if first in _CONTINUING_PREPOSITIONS:
        return len(words) == 1 and _names_only(_words(tail))
    if first in _CONTINUING_ADVERBS:
        return len(words) > 1 and words[1].casefold() in _CONTINUING_VERBS
    return first in _CONTINUING_VERBS


#: What may separate two names of one list of subjects: ", ", " and ", ", and ", " & ".
LIST_SEP = re.compile(r"\s*,?\s*(?:(?:and|&)\s+)?")


def last_run(text: str, mentions: list[tuple[int, int, bool]]) -> list[tuple[int, int, bool]]:
    """The trailing run of `mentions` (sorted, non-empty) separated only by `LIST_SEP`:
    the list of names that ends nearest the company ("B.V., K.W.K. and S.Z.")."""
    run = [mentions[-1]]
    for m in reversed(mentions[:-1]):
        if m[1] > run[0][0] or not LIST_SEP.fullmatch(text, m[1], run[0][0]):
            break
        run.insert(0, m)
    return run


#: A subject that names every author ("All authors", "each author", "all of the
#: authors"), so it names the PI (D10). Counted only when `Statement.of` is asked to
#: (`count_all_authors`): founder verification does not count it.
ALL_AUTHORS = re.compile(
    r"(?<![^\W\d_])(?:all|each)(?:\s+of\s+the)?\s+authors?(?![^\W\d_])", re.IGNORECASE
)


def accent_free(text: str) -> str:
    """`text` with each character's accents stripped where that leaves exactly one
    character, else the character as it is: the same length as `text`, so an offset found
    in the result is the same offset in `text` ("Société" -> "Societe")."""
    out = []
    for ch in text:
        base = strip_accents(ch)
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


# --- statements -------------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """A statement prepared for attribution: the original text, its normalised form with
    the map back to original offsets, its sentence spans, the PI, the other authors, the
    clause splitter, and whether "all/each author(s)" names the PI."""

    text: str
    norm: str
    origin: list[int]
    spans: list[tuple[int, int]]
    pi: Person
    others: tuple[Person, ...]
    splits: re.Pattern[str]
    count_all_authors: bool = False

    @classmethod
    def of(
        cls, text: str, forms: PiForms, authors: Sequence[object], *,
        count_all_authors: bool = False,
    ) -> Statement:
        """`authors` is the record's author list in the order `forms.position` counts
        (non-dict entries are dropped, as `locate_pi` drops them)."""
        dicts = [a for a in authors if isinstance(a, dict)]
        normalised, origin = norm_map(text)
        return cls(
            text, normalised, origin, sentence_spans(text),
            # Another author with the PI's surname: only the initials forms name the PI.
            Person(pi_regex(forms, names=not forms.surname_shared)),
            other_authors(dicts, forms), clause_splits(dicts), count_all_authors,
        )

    def sentence(self, quote: str) -> tuple[int, str] | None:
        """(offset, original text) of the one sentence that contains the normalised
        `quote`'s first occurrence; None when the quote is absent or spans sentences."""
        at = self.norm.find(quote) if quote else -1
        if at < 0:
            return None
        start, end = self.origin[at], self.origin[at + len(quote) - 1] + 1
        for s, e in self.spans:
            if s <= start and end <= e:
                return s, self.text[s:e]
        return None

    def clause(self, text: str, company: tuple[int, int]) -> tuple[int, int]:
        """The clause (`clause_splits`) holding the company span; a split inside the
        company's own name is ignored. A clause that names no author before the company
        and is subjectless (`_subjectless`) continues the subject of the clause before
        it across a ";" ("C. Bettegowda reports fees from X; and is a co-founder of
        OrisDx"), never across an eLife block start; any other ("…; the remaining authors
        are employees of Y", "…; Johns Hopkins University owns equity in Y", "…; Y funded
        this study") does not, so it names nobody."""
        splits = list(self.splits.finditer(text))
        after = [m.start() for m in splits if m.start() >= company[1]]
        before = [m for m in splits if m.end() <= company[0]]
        start = before[-1].end() if before else 0
        end, clause_end = company[0], after[0] if after else len(text)
        tail = text[company[1]:clause_end]
        named = self.mentions(text, 0, company[0])
        last_named = named[-1][0] if named else -1
        while (
            before and before[-1].group() == ";"
            and last_named < start
            and _subjectless(text[start:end], tail)
        ):
            end, tail = before.pop().start(), ""
            start = before[-1].end() if before else 0
        return start, clause_end

    def mentions(self, text: str, start: int, end: int) -> list[tuple[int, int, bool]]:
        """(start, end, names the PI) of every author mention inside text[start:end],
        sorted; with `count_all_authors`, each "all/each author(s)" counts as the PI. A PI
        mention of a relative (`_a_relative`: "Dr. Lee's spouse", "the spouse of G.L.") does
        not name the PI but still counts as a mention, so it ends a run and stops `clause`
        reaching back."""
        found = [(s, e, not _a_relative(text, s, e)) for s, e in self.pi.mentions(text)]
        if self.count_all_authors:
            found += [(m.start(), m.end(), True) for m in ALL_AUTHORS.finditer(text)]
        found += [(s, e, False) for p in self.others for s, e in p.mentions(text)]
        return sorted(m for m in found if start <= m[0] and m[1] <= end)

    def credit(self, text: str, company: tuple[int, int]) -> tuple[int, int, int, int] | None:
        """Whether `text` (a sentence as `accent_free(norm(…))`) credits the company at span
        `company` to the PI (the COI rule, D10): inside the company's clause (`clause`),
        the author mentions nearest before the company form one list (`last_run`: names
        joined only by ",", "and", "&") and that list holds a PI mention. Returns (start
        and end of the list, start and end of the PI's mention in it), or None. Nothing
        about the words is checked: "G.L. received fees from Acme Inc and Beta Bio" credits
        both companies to G.L., and "J.S. is an employee of Acme Inc" credits nothing to
        G.L. even when G.L. is named earlier in the sentence."""
        clause_start, _ = self.clause(text, company)
        before = self.mentions(text, clause_start, company[0])
        if not before:
            return None
        run = last_run(text, before)
        mine = next(((s, e) for s, e, is_pi in run if is_pi), None)
        if mine is None:
            return None
        return run[0][0], run[-1][1], mine[0], mine[1]
