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
from src.services.pubmed import (
    convert_dois_to_pmids,
    fetch_pubmed_records,
    search_pmids,
)

logger = logging.getLogger(__name__)

DEFAULT_CAP = 50
SEARCH_RETMAX = 200

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
    """Institution-distinctive tokens (generic words like "university" drop)."""
    tokens = re.findall(r"[a-z]+", affiliation.lower())
    distinctive = [
        t for t in tokens if t not in INSTITUTION_STOPWORDS and len(t) >= 4
    ]
    if distinctive:
        return distinctive
    return [t for t in tokens if len(t) >= 3]


def _aff_match(input_aff: str, paper_aff: str) -> bool:
    """Any distinctive token of the institution appears in the paper string.

    Asymmetric on purpose: paper affiliations are long (dept, address, city)
    and the input is just the institution name — needle-in-haystack recall.
    """
    if not paper_aff:
        return False
    paper_lower = paper_aff.lower()
    return any(tok in paper_lower for tok in _distinctive_aff_tokens(input_aff))


def _author_first_name_matches(
    fore_name: str | None,
    initials: str | None,
    expected_first: str,
) -> bool:
    """ForeName/Initials discrimination (strict → permissive).

    A present ForeName that does NOT start with the expected first name is a
    REJECT — this is what stops "Liu D[Author]" matching Daniel Liu when we
    want David Liu, and what caught the rehearsal's OpenAlex mislink.
    """
    if not expected_first:
        return False
    expected_first = expected_first.strip()
    expected_initial = expected_first[0].lower() if expected_first else ""
    fore = (fore_name or "").strip()
    inits = (initials or "").strip()

    if fore:
        fore_lower = fore.lower()
        if len(fore.replace(".", "").replace(" ", "")) > 1:
            return fore_lower.startswith(expected_first.lower())
        if fore_lower.startswith(expected_initial):
            return True
        return False
    if inits and inits[0].lower() == expected_initial:
        return True
    return False


def build_pubmed_query(name: str, affiliations: list[str]) -> str:
    """S4 search term: full first name when available, plus affiliation ORs."""
    parts = name.strip().split()
    if not parts:
        return ""
    last = parts[-1]
    first = parts[0] if len(parts) > 1 else ""
    if first and len(first.replace(".", "")) > 1:
        author_term = f"{last} {first}[Author]"
    elif first:
        author_term = f"{last} {first[0]}[Author]"
    else:
        author_term = f"{last}[Author]"
    if affiliations:
        aff_terms = " OR ".join(f'"{a}"[Affiliation]' for a in affiliations[:2])
        return f"({author_term}) AND ({aff_terms})"
    return author_term


def match_pi_author(record: dict, name: str) -> tuple[str, list[str]]:
    """Locate the PI in a record's author list.

    Returns ``(kind, pi_affiliations)`` where kind is ``"individual"``
    (surname + forename match; affiliations are the MATCHED author's own —
    never a co-author's, finding H2), ``"consortium"`` (no individual match
    but a CollectiveName is present), or ``"no_match"``.
    """
    parts = name.strip().split()
    last_name = parts[-1].lower() if parts else ""
    first_name = parts[0] if len(parts) > 1 else ""

    saw_collective = False
    for author in record.get("authors") or []:
        if author.get("collective"):
            saw_collective = True
            continue
        last = (author.get("last") or "").strip().lower()
        if not last or last != last_name:
            continue
        if first_name and not _author_first_name_matches(
            author.get("fore"), author.get("initials"), first_name
        ):
            continue
        return "individual", list(author.get("affiliations") or [])
    if saw_collective:
        return "consortium", []
    return "no_match", []


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
        kind, pi_affs = match_pi_author(rec, name)
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
        if rec_stages == {"s4"}:
            # Name matched, but an S4-only candidate has no ORCID anchor at
            # all — require the PI's own affiliation to match the searched
            # institution (the rehearsal's mandatory disambiguation).
            if not institution or not any(
                _aff_match(institution, a) for a in pi_affs
            ):
                dropped["identity"] += 1
                flagged.append(
                    {
                        "pmid": rec.get("pmid"),
                        "reason": "s4_affiliation_mismatch",
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
