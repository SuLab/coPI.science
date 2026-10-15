"""Multi-source publication corpus resolver (coverage design §4.1, JHU R1/R3).

The defect this replaces (D1/D2): the pipeline sourced publications from ORCID
alone, so a thin ORCID meant a thin — or hallucination-grounding — profile,
and nothing else was ever asked. Stages here:

  S1  ORCID works (curated; DOIs resolved via the D4b-verified converter)
  S2  OpenAlex by ORCID (large recall; identity NOT trusted — gated below)
  S3  PubMed ``{orcid}[auid]`` (PubMed's own ORCID-verified author field)
  S4  PubMed name+affiliation search (only when an institution is known)

Identity gates, per the 2026-08-13 rehearsal's matcher (ported from the retired sparse-data
seeding script) as upgraded by the 2026-10-15 publication-handling design (Phase 3): every
record must carry the PI as a named INDIVIDUAL author (surname + forename discrimination:
"R Lara Green" is not "Rachel Green"), OR as the author carrying the PI's ORCID iD whose
surname still matches (a variant such as "Wilson, D Scott" for "Scott Wilson" is then
identity-correct, while the surname keeps a mis-tagged iD on someone else's name out).
Names compare folded (``person_names.match_keys``: accents, ø/æ/ł, the German umlaut
expansion, hyphens, spaces and apostrophes); a particle-stripped core, a compound
surname's last part and one part of a double surname ("Garcia" for "Garcia-Lopez") match,
never a particle alone. A ``users.name`` written "Surname, Given" is read that way, packed
initials ("JD", "D.S.") are initials, and author entries PubMed marks ``ValidYN="N"`` are
ignored. On an ORCID-anchored record (S1/S3) an author list PubMed marks incomplete is kept
without the PI (``incomplete_list``), a record with no matching author is retried once with
last name and forename swapped, and an author carrying a different iD is passed over only
for a matching author without an iD or when the PI's own iD sits on another author; off an
anchor a different iD disqualifies. Several matching authors (``ambiguous``), S4-only
candidates and bare-initial matches additionally need corroboration (``_corroborated``): an
S1/S3 anchor, the matched authors' OWN affiliation matching the searched institution, or,
from S4, no affiliation at all (see ``resolve_corpus``). Consortium-only papers
(CollectiveName, no individual match) are identity-correct but are not individual lab output
and cannot consume cap slots (R1); records with neither an individual match nor a collective
are withheld AND flagged, never silently added or removed.

Two records are one paper by title only with a long enough title and years close together
(``corpus_additions.same_paper_by_title``); a dropped title duplicate is reported, so storage
excludes a stored copy.

A preprint and its journal version carry different titles or years at times, so
the title dedupe alone misses them; PubMed's own links (``update_of`` on the journal
record, ``updated_in`` on the preprint) drop the superseded version (see
``_dedupe_and_rank``).

Ranking is year DESC / PMID DESC; ``EXCLUDED_TYPES`` are skipped pre-cap (R3);
the cap is applied LAST. The cap bounds only the reported ``kept`` set; every
anchored record of ``ranked`` is stored (src/services/corpus_additions.py). A stage
failure RAISES (``CorpusStageError``) so the job retries instead of storing a thin
S1-only corpus as if it were the answer (audit M5).
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.services.corpus_additions import is_anchored, same_paper_by_title, title_key
from src.services.identifiers import normalize_orcid
from src.services.jhu_rules import is_hopkins_affiliation
from src.services.job_queue import JobDeferred
from src.services.openalex import _MAX_PAGES as _OPENALEX_MAX_PAGES
from src.services.openalex import _PER_PAGE as _OPENALEX_PER_PAGE
from src.services.openalex import fetch_works_by_orcid
from src.services.orcid import fetch_orcid_works
from src.services.person_names import (
    PARTICLES,
    fold,
    is_orcid_like,
    match_fold,
    match_keys,
    parse_person_name,
    split_comma_form,
    surname_candidates,
)
from src.services.pubmed import (
    _doi_fold,
    convert_dois_to_pmids,
    fetch_pubmed_records,
    ncbi_session,
    search_pmids,
)
from src.services.query_escaping import phrase_term, pubmed_author_term

logger = logging.getLogger(__name__)

DEFAULT_CAP = 50
# S3/S4 ESearch page size. Raised 200 -> 500 on 2026-09-22 for two measured
# reasons. (1) `search_pmids` sorts by pub date DESC, so truncation discards
# the OLDEST hits — precisely the initials-indexed pre-2000 records the
# widened `build_pubmed_query` exists to reach. (2) 200 was ALREADY binding
# before that change: the 73-PI replay baseline recorded s4 == 200 for Drew
# Pardoll and Denis Wirtz (i.e. clipped, true count unknown) and 197 for
# Andrew Pekosz. The widened query takes Rothstein from 175 to 235.
SEARCH_RETMAX = 500

#: Works fetch_works_by_orcid returns at most (its page cap); a stage-S2 count at this value
#: is read as truncated (spec 2026-10-05 §6.3 Tenure).
OPENALEX_WORKS_CAP = _OPENALEX_MAX_PAGES * _OPENALEX_PER_PAGE

# Non-research article types that cannot take cap slots (R3, motivating case
# `dang`). Single source of truth — profile_pipeline imports it from here.
EXCLUDED_TYPES = {
    "editorial",
    "comment",
    "letter",
    "news",
    "published erratum",
    # MeSH D016440 was renamed "Retraction Notice"; "Retraction of Publication" is now
    # an entry term, so both labels can appear in PubMed records.
    "retraction notice",
    "retraction of publication",
    "retracted publication",
    "expression of concern",
    "correction",
    "biography",
}

# --- ported matcher (from the retired sparse-data seeding script, validated in the
# --- 2026-08-13 backfill rehearsal: 0.13% attribution error over 1,599 adds)

INSTITUTION_STOPWORDS: frozenset[str] = frozenset({
    "university", "universite", "universidad", "of", "the", "institute",
    "institution", "research", "school", "department", "dept", "and", "for",
    "center", "centre", "college", "laboratory", "lab", "labs", "medical",
    "national", "technology", "technologies", "science", "sciences",
    "biology", "biological", "chemistry", "chemical", "engineering",
    "graduate", "program", "programs", "division", "faculty", "studies",
})


def _distinctive_aff_tokens(affiliation: str) -> list[str]:
    """Institution-distinctive tokens (generic words like "university" drop),
    in the order they appear in ``affiliation`` — order is load-bearing for
    ``_aff_match``, which requires them in that order, not merely each present."""
    tokens = re.findall(r"[a-z]+", affiliation.lower())
    distinctive = [
        t for t in tokens if t not in INSTITUTION_STOPWORDS and len(t) >= 4
    ]
    if distinctive:
        return distinctive
    return [t for t in tokens if len(t) >= 3]


def _aff_match(input_aff: str, paper_aff: str) -> bool:
    """Whether the institution's distinctive words appear in the paper affiliation
    string in order, each word-bounded, with other words allowed between them (G-8;
    D12 fix). When the institution itself is Hopkins
    (``jhu_rules.is_hopkins_affiliation``), any paper affiliation that function
    accepts also matches ("JHMI", "Bloomberg School of Public Health").

    The previous implementation ORed each distinctive token independently, so
    ``_distinctive_aff_tokens("Johns Hopkins University")`` (``["johns",
    "hopkins"]`` — "university" is a stopword) matched on "hopkins" alone:
    "Robert Wood Johnson Medical School" (via "johns"/"johnson" substring),
    "Hopkins Marine Station, Stanford University" (via "hopkins" with no
    "johns" anywhere nearby), and "Johnson & Johnson" all read as Hopkins
    affiliations. Requiring every token, in the institution's own order and
    word-bounded, rejects all three while still matching "Johns Hopkins
    University School of Medicine, Baltimore, MD" and, with gaps allowed,
    "University of Maryland School of Medicine, Baltimore" for "University of
    Maryland Baltimore".

    Still asymmetric on purpose: paper affiliations are long (dept, address,
    city) and the input is just the institution name — needle-in-haystack
    recall, just no longer a bare substring per token.
    """
    if not paper_aff:
        return False
    if is_hopkins_affiliation(input_aff) and is_hopkins_affiliation(paper_aff):
        return True
    tokens = _distinctive_aff_tokens(input_aff)
    if not tokens:
        return False
    pattern = r"\b" + r"\b.*?\b".join(re.escape(t) for t in tokens) + r"\b"
    return re.search(pattern, paper_aff, re.IGNORECASE) is not None


def _forenames_agree(author_token: str, pi_token: str) -> bool:
    """Two spelled-out first names agree when some key of each (``match_keys``) is equal,
    the author's starts with the PI's ("Rachelle" for "Rachel"), or the author's is a prefix
    of the PI's of at least 3 letters ("Bert" for "Bertram", never "Al" for "Alan")."""
    return any(
        a == p or a.startswith(p) or (len(a) >= 3 and p.startswith(a))
        for a in match_keys(author_token)
        for p in match_keys(pi_token)
    )


def _forename_initials(fore: str, initials: str | None) -> list[str] | None:
    """The initials an author entry gives, or None when its ForeName is spelled out.

    With no ForeName, the Initials field (spaces and periods dropped). A ForeName counts as
    initials when every whitespace token has at most one letter ("J D", "D. S.") or when it
    is packed: 2-4 characters once spaces and periods go, all letters, none lower-case, and
    equal (case-insensitively) to Initials whenever Initials is present ("JD", "J.D.", "DS";
    "JOHN" with Initials "J" is a name). Each initial is one letter of ``_letters``."""
    if not fore:
        return [_letters(c)[:1] for c in initials or "" if _letters(c)]
    tokens = fore.split()
    if all(len(_letters(t)) <= 1 for t in tokens):
        return [_letters(t) for t in tokens if _letters(t)]
    packed = fore.replace(" ", "").replace(".", "")
    given = (initials or "").replace(" ", "").replace(".", "")
    if (
        2 <= len(packed) <= 4
        and packed.isalpha()
        and not any(c.islower() for c in packed)
        and (not given or packed.casefold() == given.casefold())
    ):
        return [_letters(c)[:1] for c in packed if _letters(c)]
    return None


def _author_first_name_matches(
    fore_name: str | None,
    initials: str | None,
    expected_first: str,
    expected_middle: str | None = None,
) -> tuple[bool, bool]:
    """ForeName/Initials discrimination: ``(matched, bare_initial)``.

    ``bare_initial`` is True when the only identity evidence behind a match is an initial;
    item 5 (D3) has the gate corroborate it (``_corroborated``). Rules, in order (spec
    2026-10-15 R3.3), on the letters of each name part (``_letters``):

    1. An expected first name with no letters matches nothing.
    2. Initials win over a spelled-out reading (``_forename_initials``). A stored
       initial-first name ("D. Scott Wilson") is known by its middle name, so a second
       initial that differs from it ("D A", "DA", Initials "DA") is someone else; otherwise
       the first initial decides, as a bare initial.
    3. A spelled-out ForeName compares its first token only. Against a stored initial with a
       middle name, "J Marie" for "J. Marie Hardwick" is a full match (the middle name
       corroborates), "David S" a bare-initial one, and "Deborah", "Daniel R" or "Scott" are
       other people. Against a spelled-out first name, a ForeName that starts with a lone
       initial is rejected: comparing on the first initial would match "R Lara" against
       "Rachel", the rehearsal's own OpenAlex mislink (pinned by
       ``tests/unit/test_corpus.py::test_a_forename_mismatch_is_not_the_pi_even_with_the_same_surname``).
       Otherwise the first names must agree (``_forenames_agree``).
    """
    expected = _letters(expected_first)
    if not expected:
        return False, False
    e, e_is_initial = expected[0], len(expected) <= 1
    m = _letters(expected_middle)[:1]
    fore = (fore_name or "").strip()
    found = _forename_initials(fore, initials)
    if found is not None:
        if not found:
            return False, False  # an Initials-less entry, or a ForeName of punctuation only
        if e_is_initial and m and len(found) > 1 and found[1] != m:
            return False, False
        return found[0] == e, True
    tokens = [t for t in (tok.replace(".", "") for tok in fore.split()) if t]
    first = _letters(tokens[0])
    if e_is_initial and m:
        if first == e and any(
            len(_letters(t)) >= 2 and _keys_meet(t, expected_middle) for t in tokens[1:]
        ):
            return True, False
        if len(tokens) < 2 or _letters(tokens[1])[:1] != m:
            return False, False
        return first[:1] == e, True
    if e_is_initial:
        return first[:1] == e, True
    if len(first) <= 1:
        return False, False
    return _forenames_agree(tokens[0], expected_first), False


# Name particles PubMed sometimes strands at the END of ForeName rather than
# the start of LastName ("O'Neal, Anya J" indexed as LastName="Neal",
# ForeName="Anya J O'"). Case-insensitive on the trailing token; apostrophe
# variants included because that is exactly the form efetch returns.
_NAME_PARTICLES = frozenset({"o'", "d'", "mc", "mac", "de", "van", "von", "del", "la"})


def _split_particle_splice(fore: str) -> tuple[str, str] | None:
    """If ``fore`` ends in a name particle, return ``(particle, remainder)``.

    Detected by the LAST whitespace token of ``fore`` matching a known
    particle. The caller splices the particle onto the record's ``LastName``
    and re-derives the forename check from ``remainder`` — but only adopts
    the splice if it actually produces a surname match (see
    ``match_pi_author``), so a ForeName that merely happens to end in one of
    these tokens for an unrelated reason never changes anything on its own.
    Returns ``None`` when the last token is not a recognized particle.
    """
    tokens = fore.strip().split()
    if not tokens:
        return None
    if tokens[-1].lower() in _NAME_PARTICLES:
        return tokens[-1], " ".join(tokens[:-1])
    return None


#: Separators inside a surname: whitespace, "-", U+2010-U+2015, U+2212 and U+FF0D.
_SURNAME_SPLIT = re.compile(r"[\s\-‐-―−－]+")


def _letters(text: str | None) -> str:
    """The a-z and 0-9 of ``person_names.match_fold(text)``: "D." -> "d", "Jian-Feng" -> "jianfeng"."""
    return re.sub(r"[^0-9a-z]", "", match_fold(text))


def _keys_meet(a: str, b: str) -> bool:
    """Whether two name parts are equal under ``person_names.match_keys``."""
    return bool(match_keys(a) & match_keys(b))


def _surname_tokens(surname: str) -> list[str]:
    """``surname`` split on whitespace, hyphens and Unicode dashes, empty parts dropped."""
    return [t for t in _SURNAME_SPLIT.split(surname or "") if t]


def _leading_core(tokens: list[str]) -> list[str]:
    """``tokens`` without their leading particles ("van der Berg" -> ["Berg"]); the last
    token is always kept."""
    start = 0
    while start < len(tokens) - 1 and fold(tokens[start]) in PARTICLES:
        start += 1
    return tokens[start:]


def _containment_core(tokens: list[str]) -> list[str]:
    """``tokens`` without any particle, wherever it stands; empty for "De" or "van der"."""
    return [t for t in tokens if fold(t) not in PARTICLES]


def _contained(inner: list[str], outer: list[str]) -> bool:
    """Every token of ``inner`` equals (``_keys_meet``) some token of ``outer``."""
    return all(any(_keys_meet(a, b) for b in outer) for a in inner)


def _surname_matches(candidate: str, expected_last: str) -> bool:
    """Whether an author's surname is the PI's (spec 2026-10-15 R3.2). Rules, cheapest first:

    1. full join: ``match_keys`` meet, so "Hamacher Brady" = "Hamacher-Brady",
       "O’Rourke" = "O'Rourke", "Vanderberg" = "van der Berg";
    2. particle-stripped core join ("Van Dang" = "Dang");
    3. compound tail: a multi-token author surname whose last token is the PI's (item 4a),
       now across hyphens too;
    4. two-way core-token containment (F23): every non-particle token of one side equals a
       token of the other ("Garcia" for "Garcia-Lopez" and the reverse). A particle is never
       the shared token, and a lone particle surname never matches this way. The forename
       check still applies to the author (``_name_candidates``)."""
    if _keys_meet(candidate, expected_last):
        return True
    cand, exp = _surname_tokens(candidate), _surname_tokens(expected_last)
    if not cand or not exp:
        return False
    if _keys_meet("".join(_leading_core(cand)), "".join(_leading_core(exp))):
        return True
    if len(cand) >= 2 and _keys_meet(cand[-1], expected_last):
        return True
    cand_core, exp_core = _containment_core(cand), _containment_core(exp)
    return bool(cand_core and exp_core) and (
        _contained(cand_core, exp_core) or _contained(exp_core, cand_core)
    )


def build_pubmed_query(name: str, affiliations: list[str]) -> str:
    """S4 search term: full first name OR first initial, plus affiliation ORs.

    The full-name form ALONE cannot reach an initials-indexed record, which is
    most of the pre-2000 literature. Measured 2026-09-22 for Jeffrey Rothstein
    against ``"Johns Hopkins University"[Affiliation]``:

        (Rothstein Jeffrey[Author])                  175 hits,  0 of 6 landmarks
        (Rothstein J[Author])                        235 hits,  5 of 6 landmarks
        (Rothstein Jeffrey OR Rothstein J)[Author]   235 hits,  5 of 6 landmarks

    PubMed's full-author-name index only covers records that carry a full
    ``ForeName``; his 1990-1998 glutamate-transporter papers are indexed as
    "Rothstein JD" and are invisible to the full-name form. So the two are
    ORed: the full name keeps its precision where PubMed has it, the initial
    reaches everything else, and the affiliation clause plus the caller's own
    identity gate do the disambiguating the broader term gives up.

    Names and affiliations are escaped for PubMed's term syntax (`query_escaping`,
    P29), so a name cannot add a clause.
    """
    parts = pubmed_author_term(name).split()
    if not parts:
        return ""
    last = parts[-1]
    first = parts[0] if len(parts) > 1 else ""
    if first and len(first.replace(".", "")) > 1:
        author_term = (
            f"({last} {first}[Author]) OR ({last} {first[0]}[Author])"
        )
    elif first:
        author_term = f"{last} {first[0]}[Author]"
    else:
        author_term = f"{last}[Author]"
    affs = [a for a in (phrase_term(x) for x in affiliations[:2]) if a]
    if affs:
        aff_terms = " OR ".join(f'"{a}"[Affiliation]' for a in affs)
        return f"({author_term}) AND ({aff_terms})"
    return author_term


@dataclass(frozen=True)
class MatchedAuthor:
    """One author entry the name pass accepted: its 0-based ``index`` in ``record["authors"]``,
    its own affiliations, whether only an initial matched, and the iD it carries (or None)."""

    index: int
    affiliations: tuple[str, ...]
    bare_initial: bool
    orcid: str | None


@dataclass(frozen=True)
class AuthorMatch:
    """Where the PI is in a record's author list (spec 2026-10-15 R3.8). ``kind`` is one of:

    - ``"individual"``: one author is the PI; ``affiliations`` are that author's own (never a
      co-author's, finding H2) and ``index`` is its position;
    - ``"ambiguous"``: several authors match; ``affiliations`` is empty, ``candidates`` holds
      them and ``bare_initial`` is True when any matched on an initial only;
    - ``"incomplete_list"``: nobody matches and PubMed marks the list incomplete
      (``CompleteYN="N"``);
    - ``"consortium"``: nobody matches and a valid CollectiveName is listed;
    - ``"no_match"``: anything else.

    ``candidates`` holds the matched author for ``"individual"`` too."""

    kind: str
    affiliations: list[str]
    bare_initial: bool
    candidates: tuple[MatchedAuthor, ...] = ()
    index: int | None = None


@dataclass(frozen=True)
class _PiName:
    """The PI's name as the gate reads it: first and middle given names, every surname
    candidate, and the name the S4 search sends (``resolve_corpus``)."""

    first: str
    middle: str | None
    surnames: tuple[str, ...]
    search_name: str


def _pi_name(name: str) -> _PiName:
    """``name`` read for matching. "Surname, Given" (``person_names.split_comma_form``) is
    read given name first, with the whole pre-comma surname among the candidates. Otherwise
    the shared parser's derivation (spec 2026-10-05 §4.1): "Jane Doe, PhD" is a Doe, "John
    Smith Jr." a Smith, "Iris van 't Erve" also "van 't Erve", and a one-word name keeps a
    surname-only match. An ORCID-iD name has no surname, so it matches nobody."""
    comma = split_comma_form(name)
    if comma is not None:
        surname, given = comma
        ordered = f"{given} {surname}"
        surnames = surname_candidates(ordered)
        if surname not in surnames:
            surnames = (*surnames, surname)
        given_tokens = given.split()
        return _PiName(
            first=given_tokens[0],
            middle=given_tokens[1] if len(given_tokens) > 1 else None,
            surnames=surnames,
            search_name=parse_person_name(ordered).full,
        )
    full = parse_person_name(name).full
    tokens = full.split()
    if is_orcid_like(name):
        surnames: tuple[str, ...] = ()
    else:
        surnames = surname_candidates(name) or ((tokens[-1],) if tokens else ())
    return _PiName(
        first=tokens[0] if len(tokens) > 1 else "",
        middle=tokens[1] if len(tokens) > 2 else None,
        surnames=surnames,
        search_name=full,
    )


def _author_orcid(author: dict) -> str | None:
    """The iD an author entry carries, stripped, or None."""
    raw = author.get("orcid")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _usable(author: dict) -> bool:
    """A valid (``ValidYN`` not "N") individual author entry."""
    return author.get("valid", True) is not False and not author.get("collective")


def _surname_and_fore(author: dict, surnames: tuple[str, ...]) -> tuple[str, str] | None:
    """The author's (surname, forename) as compared, or None when it has no surname. Item 4b:
    a particle stranded at the end of ForeName is spliced onto the surname only if that
    turns it into a surname match; a ForeName that merely ends in a particle-like token for
    an unrelated reason changes nothing."""
    last = (author.get("last") or "").strip()
    if not last:
        return None
    fore = (author.get("fore") or "").strip()
    splice = _split_particle_splice(fore) if fore else None
    if splice:
        particle, remainder = splice
        spliced_last = f"{particle}{last}"
        if any(_surname_matches(spliced_last, s) for s in surnames):
            return spliced_last, remainder
    return last, fore


def _author_pair(author: dict, pi: _PiName, *, swapped: bool) -> tuple[str, str] | None:
    """The (surname, forename) an entry is compared by. Swapped (R3.11, F16): its ForeName as
    the surname and its LastName as the forename, both required, with no particle splice."""
    if not swapped:
        return _surname_and_fore(author, pi.surnames)
    last = (author.get("last") or "").strip()
    fore = (author.get("fore") or "").strip()
    return (fore, last) if last and fore else None


def _orcid_match(authors: list[dict], pi: _PiName, wanted: str) -> AuthorMatch | None:
    """The ORCID pass: the first valid author carrying the PI's iD whose surname matches is
    the PI, whatever the forename ("Wilson, D Scott" for "Scott Wilson"). The surname still
    has to match: publishers occasionally tag the wrong author with an iD."""
    for index, author in enumerate(authors):
        if not _usable(author) or normalize_orcid(_author_orcid(author)) != wanted:
            continue
        pair = _surname_and_fore(author, pi.surnames)
        if pair and any(_surname_matches(pair[0], s) for s in pi.surnames):
            hit = MatchedAuthor(
                index, tuple(author.get("affiliations") or ()), False, _author_orcid(author)
            )
            return AuthorMatch("individual", list(hit.affiliations), False, (hit,), index)
    return None


def _name_candidates(
    authors: list[dict], pi: _PiName, *, swapped: bool
) -> tuple[list[MatchedAuthor], bool]:
    """Every valid individual author whose surname matches one of the PI's and whose forename
    is compatible, and whether a valid CollectiveName was seen. ``swapped`` reads each entry
    with LastName and ForeName exchanged (``_author_pair``) and no Initials."""
    candidates: list[MatchedAuthor] = []
    saw_collective = False
    for index, author in enumerate(authors):
        if author.get("valid", True) is False:
            continue
        if author.get("collective"):
            saw_collective = True
            continue
        pair = _author_pair(author, pi, swapped=swapped)
        if pair is None or not any(_surname_matches(pair[0], s) for s in pi.surnames):
            continue
        bare = False
        if pi.first:
            matched, bare = _author_first_name_matches(
                pair[1], None if swapped else author.get("initials"), pi.first, pi.middle
            )
            if not matched:
                continue
        candidates.append(MatchedAuthor(
            index, tuple(author.get("affiliations") or ()), bare, _author_orcid(author)
        ))
    return candidates, saw_collective


def _drop_foreign(
    candidates: list[MatchedAuthor], authors: list[dict], wanted: str | None, *, anchored: bool
) -> list[MatchedAuthor]:
    """Q15 (R3.7). A candidate carrying an iD other than the PI's is foreign. Off an ORCID
    anchor every foreign candidate is dropped. On an anchored record (s1/s3) they are dropped
    only when a non-foreign candidate exists (tie-break 1) or the PI's own iD sits on another
    valid author entry (one the ORCID pass rejected for its surname); a different iD alone
    never disqualifies. With no usable PI iD (None, or a bad check digit) nothing is foreign."""
    if wanted is None:
        return candidates
    own = [c for c in candidates if c.orcid is None or normalize_orcid(c.orcid) == wanted]
    if not anchored or own:
        return own
    if any(_usable(a) and normalize_orcid(_author_orcid(a)) == wanted for a in authors):
        return own
    return candidates


def _spelled_out_winner(
    candidates: list[MatchedAuthor], authors: list[dict], pi: _PiName, *, swapped: bool
) -> list[MatchedAuthor]:
    """Tie-break 2 (R3.6): with several candidates and a spelled-out PI first name, the unique
    candidate whose first ForeName token is spelled out and equal to it wins."""
    if len(candidates) < 2 or len(_letters(pi.first)) <= 1:
        return candidates

    def spelled_out_and_equal(candidate: MatchedAuthor) -> bool:
        pair = _author_pair(authors[candidate.index], pi, swapped=swapped)
        tokens = [t for t in (x.replace(".", "") for x in (pair[1] if pair else "").split()) if t]
        return bool(tokens) and len(_letters(tokens[0])) >= 2 and _keys_meet(tokens[0], pi.first)

    winners = [c for c in candidates if spelled_out_and_equal(c)]
    return winners if len(winners) == 1 else candidates


def _match_pi_author_full(
    record: dict, name: str, orcid: str | None = None, *, anchored: bool = False
) -> AuthorMatch:
    """Locate the PI in a record's author list (spec 2026-10-15 R3.1-R3.8, R3.11).

    Names compare folded (``person_names.match_keys``: accents, ø/æ/ł, the German umlaut
    expansion, hyphens, spaces, apostrophes); one part of a double surname matches the whole,
    never a particle alone (``_surname_matches``); "Surname, Given" names and packed initials
    are read as such (``_pi_name``, ``_author_first_name_matches``); entries PubMed marks
    ``ValidYN="N"`` are ignored. The ORCID pass runs first (``_orcid_match``). The name pass
    then collects every candidate; on an ORCID-anchored record (``anchored``: stage s1 or s3)
    with none, it runs once more with LastName and ForeName swapped. Foreign iDs are settled
    by ``_drop_foreign`` and several candidates by ``_spelled_out_winner``; what remains is one
    author (``"individual"``) or several (``"ambiguous"``). With none, an author list PubMed
    marks incomplete is ``"incomplete_list"``, then ``"consortium"``, then ``"no_match"``."""
    pi = _pi_name(name)
    authors = list(record.get("authors") or [])
    wanted = normalize_orcid(orcid)
    if wanted and pi.surnames:
        hit = _orcid_match(authors, pi, wanted)
        if hit is not None:
            return hit
    candidates, saw_collective = _name_candidates(authors, pi, swapped=False)
    swapped = not candidates and anchored
    if swapped:
        candidates, _seen = _name_candidates(authors, pi, swapped=True)
    candidates = _drop_foreign(candidates, authors, wanted, anchored=anchored)
    candidates = _spelled_out_winner(candidates, authors, pi, swapped=swapped)
    if len(candidates) == 1:
        (only,) = candidates
        return AuthorMatch(
            "individual", list(only.affiliations), only.bare_initial, (only,), only.index
        )
    if candidates:
        return AuthorMatch(
            "ambiguous", [], any(c.bare_initial for c in candidates), tuple(candidates)
        )
    if record.get("author_list_complete", True) is False:
        return AuthorMatch("incomplete_list", [], False)
    if saw_collective:
        return AuthorMatch("consortium", [], False)
    return AuthorMatch("no_match", [], False)


def _match_pi_author_detail(
    record: dict, name: str, orcid: str | None = None, *, anchored: bool = False
) -> tuple[str, list[str], bool]:
    """``(kind, pi_affiliations, bare_initial)`` of ``_match_pi_author_full``, whose docstring
    and ``AuthorMatch`` give the rules and the five kinds. ``orcid`` is the PI's iD (optional;
    an unusable one identifies and disqualifies nobody); ``anchored`` is True for a record an
    ORCID stage (s1 or s3) returned, which enables the swap retry and the anchored iD rule."""
    match = _match_pi_author_full(record, name, orcid, anchored=anchored)
    return match.kind, list(match.affiliations), match.bare_initial


def match_pi_author(
    record: dict, name: str, orcid: str | None = None, *, anchored: bool = False
) -> tuple[str, list[str]]:
    """``(kind, pi_affiliations)``: the stable 2-tuple form of ``_match_pi_author_detail``."""
    kind, affs, _bare_initial = _match_pi_author_detail(record, name, orcid, anchored=anchored)
    return kind, affs


class CorpusStageError(RuntimeError):
    """A retrieval stage failed; the corpus must not be built without it."""


@dataclass
class CorpusResult:
    kept: list[dict[str, Any]]
    flagged: list[dict[str, Any]]
    stage_counts: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    # pmid -> the ORCID-curated DOI, so the pipeline's reconcile_pub_doi gate
    # can keep preferring the curated form as its candidate.
    orcid_dois: dict[str, str] = field(default_factory=dict)
    # Every identity-gated, deduped record BEFORE the cap, in the same
    # newest-first order as ``kept`` (``kept == ranked[:cap]``; the dicts are
    # the same objects). Storage and tenure derivation read this, not ``kept``:
    # every anchored record is stored (D14), and a PI's earliest
    # Hopkins-affiliated paper can sit outside the newest ``cap``.
    ranked: list[dict[str, Any]] = field(default_factory=list)
    # PMIDs and DOIs the strict NCBI lookups dropped as a permanent per-item
    # failure (a 4xx, an unreadable body) — records a job retry would not
    # recover, so the corpus returned normally but is known INCOMPLETE. Any
    # of them could be the PI's earliest Hopkins paper, so a paper-derived
    # tenure start from this corpus must not be persisted (profile_pipeline,
    # scripts/rederive_tenure_starts.py). Empty means nothing was dropped.
    permanently_dropped: list[str] = field(default_factory=list)
    # Stages whose search stopped at its cap ("s2": OPENALEX_WORKS_CAP works; "s3"/"s4":
    # SEARCH_RETMAX PMIDs), so the corpus may be missing older records. A paper-derived
    # tenure start from such a corpus is kept provisionally, never recorded (spec §6.3).
    truncated_stages: list[str] = field(default_factory=list)
    # (superseded PMID, surviving successor PMID) for every record the superseded-version
    # dedupe dropped; chains resolve to the final survivor. corpus_additions excludes
    # stored superseded rows and settles their pending candidates from these pairs.
    superseded: list[tuple[str, str]] = field(default_factory=list)
    # The dropped records themselves (as gated, with "stages"), in drop order. Tenure
    # derivation reads ``ranked`` plus these so a dropped preprint still dates tenure;
    # scripts/unanchored_publications_report.py must not relabel them.
    superseded_records: list[dict[str, Any]] = field(default_factory=list)
    # Spec 2026-10-09 §5.2: the S1 and S2 works this resolve fetched, as fetched, and the
    # folded DOIs of every work PubMed has (a DOI resolved to a PMID, a fetched record's own
    # DOI, a work listed with a PMID). Read only by non_pubmed_works.collect_non_pubmed_works;
    # never stored or ranked themselves.
    orcid_works: list[dict[str, Any]] = field(default_factory=list)
    openalex_works: list[dict[str, Any]] = field(default_factory=list)
    pubmed_dois: frozenset[str] = frozenset()
    #: Every PMID a stage returned in this resolve -> its sorted stages, e.g. ("s1", "s4").
    #: None on a CorpusResult resolve_corpus did not build (tests, scripts), which
    #: corpus_additions.incomplete_lookups_reason reads as incomplete, so such a result never
    #: unanchors a row (contract C4).
    stage_map: dict[str, tuple[str, ...]] | None = None
    #: The records the publication-type gate dropped, as {"pmid", "title", "year",
    #: "stages", "pub_types"}: the re-check excludes a stored row among them (C4, Q7, F12).
    type_excluded: list[dict[str, Any]] = field(default_factory=list)
    #: (duplicate PMID, keeper PMID) for every record the title dedupe dropped as the same
    #: paper as a kept one (corpus_additions.same_paper_by_title, Q13). A PMID that EFetch
    #: served twice collapses too but is not listed.
    title_duplicates: list[tuple[str, str]] = field(default_factory=list)
    #: The records of ``title_duplicates``, as gated, with "stages", in drop order: storage
    #: keeps an anchored one, excluded.
    title_duplicate_records: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Constructors that predate ``ranked`` pass only ``kept``; storage and tenure read
        # ``ranked``, so an omitted one is the kept set (a real resolve sets both).
        if not self.ranked and self.kept:
            self.ranked = list(self.kept)


def _normalize_title(title: str) -> str:
    return "".join(c for c in (title or "").lower() if c.isalnum())


def _pmid_sort_key(pmid: str) -> int:
    try:
        return int(pmid)
    except (TypeError, ValueError):
        return 0


def _settle_ambiguity(match: AuthorMatch, institution: str | None) -> AuthorMatch:
    """An ``"ambiguous"`` match whose candidates include exactly one with an affiliation that
    ``_aff_match``es ``institution`` becomes that candidate's ``"individual"`` match, with its
    own affiliations and bare flag (R3.6). Anything else is returned unchanged."""
    if match.kind != "ambiguous" or not institution:
        return match
    hits = [
        c for c in match.candidates
        if any(_aff_match(institution, a) for a in c.affiliations)
    ]
    if len(hits) != 1:
        return match
    (only,) = hits
    return AuthorMatch(
        "individual", list(only.affiliations), only.bare_initial, (only,), only.index
    )


def _corroborated(match: AuthorMatch, rec_stages: set[str], institution: str | None) -> bool:
    """Item 5/D3 corroboration for weak identity evidence: a bare-initial match, several
    matching authors (R3.6), or an S4-only record of any match. The affiliations checked are
    the matched author's own, or every candidate's for ``"ambiguous"``. True when any holds:

    - an S1/S3 anchor (ORCID-curated, or PubMed's own ORCID field);
    - an institution is set and one of those affiliations matches it (``_aff_match``);
    - an institution is set, the record came back from S4 (whose term is ``author AND
      institution``, so PubMed has already confirmed the paper is from the PI's institution)
      and no affiliation is present at all. An absent affiliation is absence of evidence:
      PubMed recorded one for the first author only until ~2014 and none before ~1988. A
      present one that does not match is evidence against, and still rejects: that is the
      case (a co-author supplies the Hopkins string) the gate exists to catch. Measured
      2026-09-22: without this route corroboration re-dropped all 12 of Rothstein's landmark
      papers. Accepted residual risk: a different J. Rothstein at Hopkins in those years."""
    if rec_stages & {"s1", "s3"}:
        return True
    if match.kind == "ambiguous":
        affs = [a for c in match.candidates for a in c.affiliations]
    else:
        affs = list(match.affiliations)
    if institution and any(_aff_match(institution, a) for a in affs):
        return True
    return bool(institution) and "s4" in rec_stages and not affs


def _gate_records(
    records: list[dict[str, Any]],
    stages: dict[str, set[str]],
    name: str,
    institution: str | None,
    orcid: str | None = None,
    type_excluded: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Identity and type gates over EFetch records: returns (kept, flagged, dropped).
    Every flagged entry's "reason" literal lives here (tests/unit/test_corpus.py). Flagged
    entries carry the record's year; ``type_excluded``, when given, receives the records the
    type gate dropped."""
    kept: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []
    dropped = {
        "consortium": 0,
        "excluded_type": 0,
        "identity": 0,
        "duplicate_title": 0,
    }

    for rec in records:
        rec_stages = stages.get(str(rec.get("pmid")), set())
        rec["stages"] = sorted(rec_stages)
        anchored = bool(rec_stages & {"s1", "s3"})
        match = _settle_ambiguity(
            _match_pi_author_full(rec, name, orcid, anchored=anchored), institution
        )
        rec["authorship"] = match.kind
        rec["pi_affiliations"] = list(match.affiliations)

        pub_types = {t.lower() for t in rec.get("pub_types") or []}
        if pub_types & EXCLUDED_TYPES:
            dropped["excluded_type"] += 1
            if type_excluded is not None:
                type_excluded.append({
                    "pmid": rec.get("pmid"), "title": rec.get("title", ""),
                    "year": rec.get("year"), "stages": rec["stages"],
                    "pub_types": list(rec.get("pub_types") or []),
                })
            continue
        if match.kind == "consortium":
            dropped["consortium"] += 1
            continue
        if match.kind == "no_match" or (match.kind == "incomplete_list" and not anchored):
            # An anchored incomplete list (R3.5) is kept with no affiliations; unanchored, the
            # missing PI is unconfirmable.
            dropped["identity"] += 1
            flagged.append(
                {
                    "pmid": rec.get("pmid"),
                    "reason": (
                        "no_individual_author_match"
                        if match.kind == "no_match"
                        else "author_list_incomplete"
                    ),
                    "title": rec.get("title", ""),
                    "stages": rec["stages"],
                    "year": rec.get("year"),
                }
            )
            continue
        weak = match.kind == "ambiguous" or match.bare_initial or rec_stages == {"s4"}
        if weak and not _corroborated(match, rec_stages, institution):
            dropped["identity"] += 1
            flagged.append(
                {
                    "pmid": rec.get("pmid"),
                    # Different evidence, kept apart in the progress text: several matching
                    # authors, an initial-only name, or an S4-only hit whose author
                    # affiliation did not match.
                    "reason": (
                        "ambiguous_author_unconfirmed"
                        if match.kind == "ambiguous"
                        else "bare_initial_unconfirmed"
                        if match.bare_initial
                        else "s4_affiliation_mismatch"
                    ),
                    "title": rec.get("title", ""),
                    "stages": rec["stages"],
                    "year": rec.get("year"),
                }
            )
            continue
        kept.append(rec)
    return kept, flagged, dropped


def _drop_superseded(
    survivors: list[dict[str, Any]],
    dropped: dict[str, int],
    superseded: list[tuple[dict[str, Any], str]] | None,
) -> list[dict[str, Any]]:
    """Drop superseded versions from ``survivors``; counts each into
    ``dropped["duplicate_title"]`` and, when ``superseded`` is a list, appends
    (dropped record, PMID of its final surviving successor) in drop order."""
    # X is superseded by Y when Y's ``update_of`` names X's PMID, or X is a Preprint whose
    # ``updated_in`` names Y's PMID. These links survive title and year differences the
    # title dedupe cannot see. An anchored X is never dropped for an unanchored Y: Y is
    # only a review candidate (corpus_additions), so dropping X would leave the work
    # unstored. Pairs run in numeric PMID order. A pair is skipped when X is already
    # dropped, or when Y's chain of successors already leads back to X (a mutual link or
    # longer cycle), so a cycle keeps one record rather than none; a chain A->B->C keeps
    # only C whatever order its links are met in.
    by_pmid = {str(r.get("pmid")): r for r in survivors if r.get("pmid")}
    pairs: set[tuple[str, str]] = set()  # (superseded pmid, superseding pmid)
    for y_pmid, y in by_pmid.items():
        for x_pmid in y.get("update_of") or []:
            pairs.add((str(x_pmid), y_pmid))
    for x_pmid, x in by_pmid.items():
        if "preprint" in {str(t).lower() for t in x.get("pub_types") or []}:
            for y_pmid in x.get("updated_in") or []:
                pairs.add((x_pmid, str(y_pmid)))

    dropped_by: dict[str, str] = {}  # superseded pmid -> the pmid that superseded it

    def survivor_of(pmid: str) -> str:
        while pmid in dropped_by:
            pmid = dropped_by[pmid]
        return pmid

    for x_pmid, y_pmid in sorted(
        pairs, key=lambda p: (_pmid_sort_key(p[0]), _pmid_sort_key(p[1]))
    ):
        x, y = by_pmid.get(x_pmid), by_pmid.get(y_pmid)
        if x is None or y is None or x_pmid == y_pmid:
            continue
        if x_pmid in dropped_by or survivor_of(y_pmid) == x_pmid:
            continue
        if is_anchored(x) and not is_anchored(y):
            continue
        dropped_by[x_pmid] = y_pmid
        dropped["duplicate_title"] += 1
    if not dropped_by:
        return survivors
    if superseded is not None:
        superseded.extend((by_pmid[x], survivor_of(x)) for x in dropped_by)
    return [r for r in survivors if str(r.get("pmid")) not in dropped_by]


def _dedupe_and_rank(
    kept: list[dict[str, Any]],
    dropped: dict[str, int],
    superseded: list[tuple[dict[str, Any], str]] | None = None,
    title_duplicates: list[tuple[dict[str, Any], str]] | None = None,
) -> list[dict[str, Any]]:
    """PMID dedupe, same-paper title dedupe, superseded-version dedupe and newest-first
    ranking (pre-cap); counts every record it drops into ``dropped["duplicate_title"]``.

    1. Records sharing a PMID (EFetch served it twice) collapse to one: the higher year, on a
       tie the first seen. Not reported.
    2. Walking the rest in keeper preference (anchored first, then year descending with
       None last, then PMID descending), a record that is the same paper by title
       (``corpus_additions.same_paper_by_title``: a title key of at least
       TITLE_KEY_MIN_CHARS characters, years at most SAME_PAPER_YEARS apart) as an
       already-kept record is dropped as a duplicate of the first such keeper; when
       ``title_duplicates`` is a list it receives (record, keeper PMID). A short title such
       as "Introduction", or years further apart, never collapses, and an anchored record
       never loses to an unanchored one.
    3. ``_drop_superseded`` (``superseded`` receives its (record, final successor PMID)
       pairs), then year DESC / PMID DESC."""
    by_pmid: dict[str, dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    for rec in kept:
        pmid = rec.get("pmid")
        if not pmid:
            others.append(rec)
            continue
        rival = by_pmid.get(str(pmid))
        if rival is None:
            by_pmid[str(pmid)] = rec
            continue
        dropped["duplicate_title"] += 1
        if (rec.get("year") or 0) > (rival.get("year") or 0):
            by_pmid[str(pmid)] = rec
    preference = sorted([*by_pmid.values(), *others], key=lambda r: (
        not is_anchored(r), r.get("year") is None, -(r.get("year") or 0),
        -_pmid_sort_key(r.get("pmid"))))
    survivors: list[dict[str, Any]] = []
    keepers: dict[str, list[dict[str, Any]]] = {}
    for rec in preference:
        key = title_key(rec.get("title"))
        keeper = next((k for k in keepers.get(key, []) if same_paper_by_title(
            k.get("title"), k.get("year"), rec.get("title"), rec.get("year"))),
            None) if key else None
        if keeper is None:
            survivors.append(rec)
            if key:
                keepers.setdefault(key, []).append(rec)
            continue
        dropped["duplicate_title"] += 1
        if title_duplicates is not None:
            title_duplicates.append((rec, str(keeper.get("pmid"))))
    survivors = _drop_superseded(survivors, dropped, superseded)
    return sorted(
        survivors,
        key=lambda r: (r.get("year") or 0, _pmid_sort_key(r.get("pmid"))),
        reverse=True,
    )


def _attribute_resolved_dois(
    mapping: dict[str, str],
    doi_pool: dict[str, str],
    orcid_doi_only: dict[str, str],
    orcid_dois: dict[str, str],
    add: Callable[[str | None, str], None],
) -> None:
    """Credit each DOI-resolved PMID to the stage that proposed its DOI."""
    for doi, pmid in mapping.items():
        stage = doi_pool.get(doi)
        if stage is None:
            # convert_dois_to_pmids guarantees its keys are the caller's
            # own forms; when that contract broke (the converter echoed
            # lowercase, 2026-08-25) a bracket lookup here killed the
            # whole profile job. Losing one attribution must never cost
            # the corpus — skip it loudly instead.
            logger.warning(
                "doi_resolution returned %r, which is not a doi_pool "
                "key; skipping it",
                doi,
            )
            continue
        add(pmid, stage)
        if doi in orcid_doi_only:
            orcid_dois[str(pmid)] = doi


def _pubmed_dois(
    mapping: dict[str, str], records: list[dict[str, Any]], *works: list[dict[str, Any]],
) -> frozenset[str]:
    """Folded DOIs this resolve ties to a PubMed record: a DOI resolved to a PMID, a fetched
    record's own DOI, a work listed with a PMID. The non-PubMed corpus skips them."""
    dois = [*mapping, *(r.get("doi") for r in records)]
    dois += [w.get("doi") for group in works for w in group if w.get("pmid")]
    return frozenset(_doi_fold(d) for d in dois if d)


async def resolve_corpus(
    orcid: str,
    name: str,
    institution: str | None,
    *,
    cap: int = DEFAULT_CAP,
) -> CorpusResult:
    """Resolve, gate, dedupe, rank and cap a PI's publication corpus.

    Every retrieval stage runs inside ``_stage``, so any failure it raises
    becomes ``CorpusStageError`` and no corpus is built. The three lookups
    that swallow failures by default run with ``strict=True``:
    ``fetch_orcid_works`` then raises on anything but a record-state status
    (``_RECORD_STATE_STATUSES``: 301, 404, 409, 410); the two NCBI lookups
    raise on a transient failure or a bug, while a permanent per-item failure
    drops only that PMID/DOI and is reported in
    ``CorpusResult.permanently_dropped`` rather than hidden (a run of
    ``_SYSTEMIC_RUN`` identical ones raises instead).

    ``kept`` is the newest ``cap`` records for reporting; storage reads ``ranked``
    (spec 2026-10-05 §6.3, D14). A stage whose search stopped at its cap is listed in
    ``truncated_stages``.
    """

    stages: dict[str, set[str]] = {}
    doi_pool: dict[str, str] = {}  # doi -> first stage that proposed it

    def _add(pmid: str | None, stage: str) -> None:
        if pmid:
            stages.setdefault(str(pmid), set()).add(stage)

    async def _stage(stage_name: str, coro):
        try:
            return await coro
        except JobDeferred:
            raise  # Budget deferral must not consume a job's retry attempt.
        except Exception as exc:
            raise CorpusStageError(
                f"corpus stage {stage_name} failed: {exc}"
            ) from exc

    stage_counts: dict[str, int] = {}
    permanently_dropped: list[str] = []
    truncated: list[str] = []

    orcid_dois: dict[str, str] = {}
    orcid_doi_only: dict[str, str] = {}  # doi -> "" until resolved to a pmid

    async with ncbi_session():
        orcid_works = await _stage(
            "s1_orcid_works", fetch_orcid_works(orcid, strict=True)
        )
        for w in orcid_works:
            if w.get("pmid"):
                _add(w["pmid"], "s1")
                if w.get("doi"):
                    orcid_dois[str(w["pmid"])] = w["doi"]
            elif w.get("doi"):
                doi_pool.setdefault(w["doi"], "s1")
                orcid_doi_only[w["doi"]] = ""
        stage_counts["s1"] = len(orcid_works)

        openalex_works = await _stage("s2_openalex", fetch_works_by_orcid(orcid))
        for w in openalex_works:
            if w.get("pmid"):
                _add(w["pmid"], "s2")
            elif w.get("doi"):
                doi_pool.setdefault(w["doi"], "s2")
        stage_counts["s2"] = len(openalex_works)
        if len(openalex_works) >= OPENALEX_WORKS_CAP:
            truncated.append("s2")

        s3_pmids = await _stage(
            "s3_pubmed_auid", search_pmids(f"{orcid}[auid]", retmax=SEARCH_RETMAX)
        )
        for pmid in s3_pmids:
            _add(pmid, "s3")
        stage_counts["s3"] = len(s3_pmids)
        if len(s3_pmids) >= SEARCH_RETMAX:
            truncated.append("s3")

        if institution and institution.strip():
            term = build_pubmed_query(_pi_name(name).search_name, [institution])
            s4_pmids = await _stage(
                "s4_pubmed_name_affiliation",
                search_pmids(term, retmax=SEARCH_RETMAX),
            )
            for pmid in s4_pmids:
                _add(pmid, "s4")
            stage_counts["s4"] = len(s4_pmids)
            if len(s4_pmids) >= SEARCH_RETMAX:
                truncated.append("s4")
        else:
            # R4: a missing institution silently disables S4 — say so loudly.
            logger.warning(
                "resolve_corpus(%s): no institution on file, S4 skipped", orcid
            )
            stage_counts["s4"] = 0

        mapping: dict[str, str] = {}
        if doi_pool:
            mapping = await _stage(
                "doi_resolution",
                convert_dois_to_pmids(
                    list(doi_pool), strict=True,
                    permanently_dropped=permanently_dropped,
                ),
            )
            _attribute_resolved_dois(
                mapping, doi_pool, orcid_doi_only, orcid_dois, _add
            )

        # Every PMID is fetched here, including those DOI verification already
        # fetched: EFetch's own order and its records under another PMID decide
        # ``records``, hence ``flagged`` and the dedupe, and only the same requests
        # reproduce them (Freeze B24; tests/unit/test_doi_resolution_batching.py).
        records = (
            await _stage(
                "efetch",
                fetch_pubmed_records(
                    list(stages), strict=True,
                    permanently_dropped=permanently_dropped,
                ),
            )
            if stages
            else []
        )

    # A DOI whose lookup failed per-item is not missing if its paper arrived
    # anyway, through another stage's PMID: EFetch returned a record carrying
    # that DOI. Only a DOI with no such record leaves the corpus incomplete.
    fetched_dois = {_doi_fold(r["doi"]) for r in records if r.get("doi")}
    permanently_dropped[:] = [
        item for item in permanently_dropped
        if item not in doi_pool or _doi_fold(item) not in fetched_dois
    ]

    type_excluded: list[dict[str, Any]] = []
    kept, flagged, dropped = _gate_records(
        records, stages, name, institution, orcid, type_excluded=type_excluded
    )
    superseded_pairs: list[tuple[dict[str, Any], str]] = []
    title_dups: list[tuple[dict[str, Any], str]] = []
    ranked = _dedupe_and_rank(kept, dropped, superseded_pairs, title_dups)
    kept = ranked[:cap]  # the cap is applied LAST

    logger.info(
        "resolve_corpus(%s): stages=%s kept=%d flagged=%d dropped=%s "
        "permanently_dropped=%d truncated=%s",
        orcid, stage_counts, len(kept), len(flagged), dropped,
        len(permanently_dropped), truncated,
    )
    return CorpusResult(
        kept=kept,
        flagged=flagged,
        stage_counts=stage_counts,
        dropped=dropped,
        orcid_dois=orcid_dois,
        ranked=ranked,
        permanently_dropped=permanently_dropped,
        truncated_stages=truncated,
        superseded=[(str(r.get("pmid")), succ) for r, succ in superseded_pairs],
        superseded_records=[r for r, _succ in superseded_pairs],
        orcid_works=list(orcid_works),
        openalex_works=list(openalex_works),
        pubmed_dois=_pubmed_dois(mapping, records, orcid_works, openalex_works),
        stage_map={pmid: tuple(sorted(s)) for pmid, s in stages.items()},
        type_excluded=type_excluded,
        title_duplicates=[(str(r.get("pmid")), keeper) for r, keeper in title_dups],
        title_duplicate_records=[r for r, _keeper in title_dups],
    )
