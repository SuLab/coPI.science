"""Multi-source publication corpus resolver (coverage design §4.1, JHU R1/R3).

The defect this replaces (D1/D2): the pipeline sourced publications from ORCID
alone, so a thin ORCID meant a thin — or hallucination-grounding — profile,
and nothing else was ever asked. Stages here:

  S1  ORCID works (curated; DOIs resolved via the D4b-verified converter)
  S2  OpenAlex by ORCID (large recall; identity NOT trusted — gated below)
  S3  PubMed ``{orcid}[auid]`` (PubMed's own ORCID-verified author field)
  S4  PubMed name+affiliation search (only when an institution is known)

Identity gates, per the 2026-08-13 rehearsal's matcher (ported from
``scripts/generate_sparsedata_user.py``): every record must carry the PI as a
named INDIVIDUAL author (surname + forename discrimination — "R Lara Green" is
not "Rachel Green"); S4-only candidates additionally need the PI's OWN
affiliation to match the searched institution. Consortium-only papers
(CollectiveName, no individual match) are identity-correct but are not
individual lab output and cannot consume cap slots (R1); records with neither
an individual match nor a collective are withheld AND flagged, never silently
added or removed.

Ranking is year DESC / PMID DESC; ``EXCLUDED_TYPES`` are skipped pre-cap (R3);
the cap is applied LAST. A stage failure RAISES (``CorpusStageError``) so the
job retries instead of storing a thin S1-only corpus as if it were the answer
(audit M5).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.services.openalex import fetch_works_by_orcid
from src.services.orcid import fetch_orcid_works
from src.services.patents import _to_ascii
from src.services.pubmed import (
    convert_dois_to_pmids,
    fetch_pubmed_records,
    search_pmids,
)

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

# Non-research article types that cannot take cap slots (R3, motivating case
# `dang`). Single source of truth — profile_pipeline imports it from here.
EXCLUDED_TYPES = {
    "editorial",
    "comment",
    "letter",
    "news",
    "published erratum",
    "retraction of publication",
    "correction",
    "biography",
}

# --- ported matcher (scripts/generate_sparsedata_user.py, validated in the
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
    ``_aff_match``, which requires them adjacent, not merely each present."""
    tokens = re.findall(r"[a-z]+", affiliation.lower())
    distinctive = [
        t for t in tokens if t not in INSTITUTION_STOPWORDS and len(t) >= 4
    ]
    if distinctive:
        return distinctive
    return [t for t in tokens if len(t) >= 3]


def _aff_match(input_aff: str, paper_aff: str) -> bool:
    """Whether the institution's distinctive words appear TOGETHER, in order,
    as a word-bounded phrase in the paper affiliation string (D12 fix).

    The previous implementation ORed each distinctive token independently, so
    ``_distinctive_aff_tokens("Johns Hopkins University")`` (``["johns",
    "hopkins"]`` — "university" is a stopword) matched on "hopkins" alone:
    "Robert Wood Johnson Medical School" (via "johns"/"johnson" substring),
    "Hopkins Marine Station, Stanford University" (via "hopkins" with no
    "johns" anywhere nearby), and "Johnson & Johnson" all read as Hopkins
    affiliations. Requiring the tokens ADJACENT, in the institution's own
    order, and word-bounded — the same phrase/word-bounded style as
    ``jhu_rules.is_hopkins_affiliation`` — rejects all three while still
    matching "Johns Hopkins University School of Medicine, Baltimore, MD".

    Still asymmetric on purpose: paper affiliations are long (dept, address,
    city) and the input is just the institution name — needle-in-haystack
    recall, just no longer a bare substring per token.
    """
    if not paper_aff:
        return False
    tokens = _distinctive_aff_tokens(input_aff)
    if not tokens:
        return False
    pattern = r"\b" + r"[\s,.\-]+".join(re.escape(t) for t in tokens) + r"\b"
    return re.search(pattern, paper_aff, re.IGNORECASE) is not None


def _author_first_name_matches(
    fore_name: str | None,
    initials: str | None,
    expected_first: str,
    expected_middle: str | None = None,
) -> tuple[bool, bool]:
    """ForeName/Initials discrimination (strict -> permissive).

    Returns ``(matched, bare_initial)``. ``bare_initial`` is True when the
    ONLY identity evidence behind a positive match is a single letter — no
    full given name and no corroborating middle name — which item 5 (D3)
    requires the caller to additionally corroborate (own-affiliation or an
    S1/S3 anchor) before trusting.

    A present ForeName that does NOT start with the expected first name is a
    REJECT — this is what stops "Liu D[Author]" matching Daniel Liu when we
    want David Liu, and what caught the rehearsal's OpenAlex mislink. Two
    permissive paths sit in front of that strict check, each added for a
    real PubMed indexing habit (D2):

    1. Spaced-initials ForeName ("J D", "A L"): PubMed spells out multiple
       initials as space-separated single letters rather than one string.
       Every token being a single letter (after stripping periods) means
       "this is initials, not a given name" regardless of token count, so
       compare on the first initial rather than a doomed ``startswith``
       ("j d".startswith("jeffrey") is always False).
    2. Initial-plus-name ForeName ("J Marie" against a stored first name of
       the single token "J."): guarded NARROWLY, on purpose — a blanket
       "compare on the first initial" rule here would also match "R Lara"
       against "Rachel" (the rehearsal's own OpenAlex mislink, pinned by
       ``tests/unit/test_corpus.py::test_a_forename_mismatch_is_not_the_pi_even_with_the_same_surname``).
       This path fires ONLY when the EXPECTED first name is ITSELF an
       initial, and additionally requires the forename's non-initial token
       to equal the expected middle name — "Marie" corroborates "J Marie" is
       J. Marie Hardwick, not just anyone whose first name starts with J.
       Without an expected middle name to check, this path does not apply
       and control falls through to the strict ``startswith`` below (which
       rejects, as before).
    """
    if not expected_first:
        return False, False
    expected_first = expected_first.strip()
    expected_initial = expected_first[0].lower() if expected_first else ""
    expected_first_is_initial = len(expected_first.replace(".", "")) <= 1
    fore = (fore_name or "").strip()
    inits = (initials or "").strip()

    if fore:
        fore_tokens = fore.split()
        stripped_tokens = [t.replace(".", "") for t in fore_tokens]
        if stripped_tokens and all(len(t) <= 1 for t in stripped_tokens):
            # Item 1: spaced initials — the whole ForeName is initials.
            return stripped_tokens[0].lower() == expected_initial, True

        if expected_first_is_initial and expected_middle:
            # Item 2: initial-plus-name, corroborated by the middle name.
            if stripped_tokens and stripped_tokens[0].lower() == expected_initial:
                remainder = [t for t in stripped_tokens[1:] if len(t) > 1]
                if any(
                    t.lower() == expected_middle.strip().lower() for t in remainder
                ):
                    return True, False
            # Falls through to the strict check below on no match — this
            # path never itself REJECTS, it only ever adds an acceptance.

        fore_lower = fore.lower()
        if len(fore.replace(".", "").replace(" ", "")) > 1:
            return fore_lower.startswith(expected_first.lower()), False
        if fore_lower.startswith(expected_initial):
            return True, True
        return False, False
    if inits and inits[0].lower() == expected_initial:
        return True, True
    return False, False


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


# German-only expansion (D2(b)/item 3): rewrites ONLY the umlaut/eszett
# characters themselves, never any plain ASCII letter — an ASCII-spelled
# surname must never gain an umlaut-expanded variant, or an unrelated ASCII
# namesake (Hermann Joseph *Muller*) would collide with the accented one
# (Ulrich *Mueller*, PubMed-indexed "Müller"). A string with none of these
# four characters passes through every variant below unchanged.
_GERMAN_EXPANSION = str.maketrans(
    {"ü": "ue", "ö": "oe", "ä": "ae", "ß": "ss",
     "Ü": "Ue", "Ö": "Oe", "Ä": "Ae"}
)


def _surname_variants(surname: str) -> set[str]:
    """Comparable spellings of a surname (D2(b)/item 3: diacritic folding).

    Three independently-derived forms — never chained — so folding one way
    cannot also produce the other side's false positive:

    - raw, lowercased;
    - NFKD/accent-stripped, reusing ``patents._to_ascii`` for that half only
      ("Müller" -> "Muller" — accent-stripping alone, NOT the German
      expansion below, which ``_to_ascii`` does not perform);
    - the German umlaut/eszett expansion applied to the ORIGINAL string
      ("Müller" -> "Mueller"), so that variant is generated only when the
      original text actually carries one of those four characters.

    Expected "Mueller" (plain ASCII, no umlaut) therefore yields only
    ``{"mueller"}`` — it never generates "Muller", which is the asymmetry
    item 3 requires. A real "Müller" record yields
    ``{"müller", "muller", "mueller"}``, intersecting expected "Mueller"'s
    ``{"mueller"}``.
    """
    if not surname:
        return set()
    raw = surname.strip()
    if not raw:
        return set()
    return {
        raw.lower(),
        _to_ascii(raw).lower(),
        raw.translate(_GERMAN_EXPANSION).lower(),
    }


def _surname_matches(candidate: str, expected_last: str) -> bool:
    """Whole-surname match (with folding), OR a compound surname whose LAST
    whitespace token is the PI's surname (item 4a: "Van Dang, Chi V" vs "Chi
    Dang")."""
    if _surname_variants(candidate) & _surname_variants(expected_last):
        return True
    tokens = candidate.split()
    return len(tokens) > 1 and bool(
        _surname_variants(tokens[-1]) & _surname_variants(expected_last)
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
    """
    parts = name.strip().split()
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
    if affiliations:
        aff_terms = " OR ".join(f'"{a}"[Affiliation]' for a in affiliations[:2])
        return f"({author_term}) AND ({aff_terms})"
    return author_term


def _match_pi_author_detail(record: dict, name: str) -> tuple[str, list[str], bool]:
    """Locate the PI in a record's author list (full detail).

    Returns ``(kind, pi_affiliations, bare_initial)``. ``kind`` is
    ``"individual"`` (surname + forename match; affiliations are the MATCHED
    author's own — never a co-author's, finding H2), ``"consortium"`` (no
    individual match but a CollectiveName is present), or ``"no_match"``.
    ``bare_initial`` is True only for an ``"individual"`` match whose sole
    forename evidence was a single letter (item 5/D3) — callers that need
    that distinction (``resolve_corpus``) use this function directly;
    ``match_pi_author`` is the stable 2-tuple form everything else keeps
    using.
    """
    parts = name.strip().split()
    last_name = parts[-1].lower() if parts else ""
    first_name = parts[0] if len(parts) > 1 else ""
    middle_name = parts[1] if len(parts) > 2 else None

    saw_collective = False
    for author in record.get("authors") or []:
        if author.get("collective"):
            saw_collective = True
            continue
        last = (author.get("last") or "").strip()
        if not last:
            continue
        fore = (author.get("fore") or "").strip()

        candidate_last, candidate_fore = last, fore
        splice = _split_particle_splice(fore) if fore else None
        if splice:
            particle, remainder = splice
            spliced_last = f"{particle}{last}"
            # Item 4b: only adopt the splice if it actually turns into a
            # surname match — a ForeName that merely ends in a particle-like
            # token for an unrelated reason must change nothing.
            if _surname_matches(spliced_last, last_name):
                candidate_last, candidate_fore = spliced_last, remainder

        if not _surname_matches(candidate_last, last_name):
            continue

        bare_initial = False
        if first_name:
            matched, bare_initial = _author_first_name_matches(
                candidate_fore, author.get("initials"), first_name, middle_name
            )
            if not matched:
                continue
        return "individual", list(author.get("affiliations") or []), bare_initial
    if saw_collective:
        return "consortium", [], False
    return "no_match", [], False


def match_pi_author(record: dict, name: str) -> tuple[str, list[str]]:
    """Locate the PI in a record's author list.

    Returns ``(kind, pi_affiliations)`` where kind is ``"individual"``
    (surname + forename match; affiliations are the MATCHED author's own —
    never a co-author's, finding H2), ``"consortium"`` (no individual match
    but a CollectiveName is present), or ``"no_match"``. See
    ``_match_pi_author_detail`` for the bare-initial corroboration flag
    ``resolve_corpus`` needs and this wrapper drops.
    """
    kind, affs, _bare_initial = _match_pi_author_detail(record, name)
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


def _normalize_title(title: str) -> str:
    return "".join(c for c in (title or "").lower() if c.isalnum())


def _pmid_sort_key(pmid: str) -> int:
    try:
        return int(pmid)
    except (TypeError, ValueError):
        return 0


async def resolve_corpus(
    orcid: str,
    name: str,
    institution: str | None,
    *,
    cap: int = DEFAULT_CAP,
) -> CorpusResult:
    """Resolve, gate, dedupe, rank and cap a PI's publication corpus."""

    stages: dict[str, set[str]] = {}
    doi_pool: dict[str, str] = {}  # doi -> first stage that proposed it

    def _add(pmid: str | None, stage: str) -> None:
        if pmid:
            stages.setdefault(str(pmid), set()).add(stage)

    async def _stage(stage_name: str, coro):
        try:
            return await coro
        except Exception as exc:
            raise CorpusStageError(
                f"corpus stage {stage_name} failed: {exc}"
            ) from exc

    stage_counts: dict[str, int] = {}

    orcid_dois: dict[str, str] = {}
    orcid_doi_only: dict[str, str] = {}  # doi -> "" until resolved to a pmid

    orcid_works = await _stage("s1_orcid_works", fetch_orcid_works(orcid))
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

    s3_pmids = await _stage(
        "s3_pubmed_auid", search_pmids(f"{orcid}[auid]", retmax=SEARCH_RETMAX)
    )
    for pmid in s3_pmids:
        _add(pmid, "s3")
    stage_counts["s3"] = len(s3_pmids)

    if institution and institution.strip():
        term = build_pubmed_query(name, [institution])
        s4_pmids = await _stage(
            "s4_pubmed_name_affiliation",
            search_pmids(term, retmax=SEARCH_RETMAX),
        )
        for pmid in s4_pmids:
            _add(pmid, "s4")
        stage_counts["s4"] = len(s4_pmids)
    else:
        # R4: a missing institution silently disables S4 — say so loudly.
        logger.warning(
            "resolve_corpus(%s): no institution on file, S4 skipped", orcid
        )
        stage_counts["s4"] = 0

    if doi_pool:
        mapping = await _stage(
            "doi_resolution", convert_dois_to_pmids(list(doi_pool))
        )
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
            _add(pmid, stage)
            if doi in orcid_doi_only:
                orcid_dois[str(pmid)] = doi

    records = (
        await _stage("efetch", fetch_pubmed_records(list(stages)))
        if stages
        else []
    )

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
        kind, pi_affs, bare_initial = _match_pi_author_detail(rec, name)
        rec["authorship"] = kind
        rec["pi_affiliations"] = pi_affs

        pub_types = {t.lower() for t in rec.get("pub_types") or []}
        if pub_types & EXCLUDED_TYPES:
            dropped["excluded_type"] += 1
            continue
        if kind == "consortium":
            dropped["consortium"] += 1
            continue
        if kind == "no_match":
            dropped["identity"] += 1
            flagged.append(
                {
                    "pmid": rec.get("pmid"),
                    "reason": "no_individual_author_match",
                    "title": rec.get("title", ""),
                    "stages": rec["stages"],
                }
            )
            continue
        # Item 5/D3: a single-letter forename is not, on its own, enough
        # identity evidence to trust — corroboration is now required on ANY
        # stage combination for a bare-initial match, not just the old
        # S4-only gate. The S4-only gate itself is KEPT for a full-forename
        # match too (an S4-only candidate has no ORCID anchor at all, so an
        # affiliation match is the rehearsal's own mandatory disambiguation
        # regardless of how specific the name match was) — ``rec_stages ==
        # {"s4"}`` is folded into the same check below since it can never be
        # "anchored" (s1/s3 are absent from the set by construction).
        if bare_initial or rec_stages == {"s4"}:
            anchored = bool(rec_stages & {"s1", "s3"})
            aff_confirmed = bool(institution) and any(
                _aff_match(institution, a) for a in pi_affs
            )
            # Third route, and the one that carries the pre-2000 corpus:
            # the record came back from S4 — whose term is `author AND
            # institution` (`build_pubmed_query`), so PubMed has already
            # confirmed the paper is FROM the PI's institution — AND the
            # matched author entry carries NO affiliation string at all.
            #
            # The `not pi_affs` clause is what keeps this from gutting the
            # rehearsal's S4-only disambiguation. An ABSENT affiliation is
            # absence of evidence: PubMed recorded an affiliation for the
            # first author only until ~2014 and none whatsoever before
            # ~1988, so a last-author 1996 paper can never satisfy the
            # strict `aff_confirmed` form no matter who wrote it. A PRESENT
            # affiliation that does not match is evidence against, and still
            # rejects — which is the case (a co-author supplies the Hopkins
            # string while the named author sits elsewhere) the original
            # gate existed to catch.
            #
            # Measured 2026-09-22: without this route, corroboration
            # re-dropped every one of Rothstein's 12 landmark papers that
            # items 1-4 had just recovered — the widening and the
            # containment exactly cancelling. Residual risk, accepted and
            # recorded in the plan: a DIFFERENT J. Rothstein publishing out
            # of Johns Hopkins in those same years would also pass. Surname
            # + first initial + institution-on-the-paper is the strongest
            # signal these records physically carry.
            inst_searched = (
                bool(institution) and "s4" in rec_stages and not pi_affs
            )
            if not anchored and not aff_confirmed and not inst_searched:
                dropped["identity"] += 1
                flagged.append(
                    {
                        "pmid": rec.get("pmid"),
                        "reason": "bare_initial_unconfirmed",
                        "title": rec.get("title", ""),
                        "stages": rec["stages"],
                    }
                )
                continue
        kept.append(rec)

    # Dedupe by normalized title (PMID dedupe fell out of the stages map).
    # Preprint/journal pairs share a title; keep the later year (the journal
    # version), tiebreak higher PMID. Errata never reach here (EXCLUDED_TYPES).
    by_title: dict[str, dict] = {}
    for rec in kept:
        key = _normalize_title(rec.get("title", "")) or f"pmid:{rec.get('pmid')}"
        rival = by_title.get(key)
        if rival is None:
            by_title[key] = rec
        else:
            dropped["duplicate_title"] += 1
            winner = max(
                (rival, rec),
                key=lambda r: (r.get("year") or 0, _pmid_sort_key(r.get("pmid"))),
            )
            by_title[key] = winner

    ranked = sorted(
        by_title.values(),
        key=lambda r: (r.get("year") or 0, _pmid_sort_key(r.get("pmid"))),
        reverse=True,
    )
    kept = ranked[:cap]  # the cap is applied LAST

    logger.info(
        "resolve_corpus(%s): stages=%s kept=%d flagged=%d dropped=%s",
        orcid, stage_counts, len(kept), len(flagged), dropped,
    )
    return CorpusResult(
        kept=kept,
        flagged=flagged,
        stage_counts=stage_counts,
        dropped=dropped,
        orcid_dois=orcid_dois,
    )
