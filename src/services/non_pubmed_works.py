"""Non-PubMed works for a PubMed-thin PI (spec 2026-10-09 §5, Phase B).

The corpus (``corpus.py``) is PubMed-only: every row it stores carries a PMID, so a PI whose
work lives in conference proceedings, arXiv or astrophysics journals had nothing stored and a
profile written from the model's own knowledge. This module turns the works a resolve already
fetched — the PI's ORCID list (S1, ``CorpusResult.orcid_works``) and OpenAlex's works for the
ORCID (S2, ``CorpusResult.openalex_works``) — into storable records for the works PubMed does
not have. The pipeline stores them only for a PubMed-thin PI (``tenure_scope.pubmed_thin``),
through ``corpus_additions.apply_non_pubmed_works``.

Owner decisions (spec §2): D5, store DOI-bearing works with OpenAlex abstracts; D6, store
ORCID-listed works AND every OpenAlex work attributed to the ORCID, without staff review —
the namesake and duplicate risk is accepted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from src.models.publication import PROVENANCE_OPENALEX
from src.services.corpus import CorpusResult, _normalize_title
from src.services.openalex import fetch_work_by_doi
from src.services.pubmed import _doi_fold, normalize_doi

#: OpenAlex ``type`` values kept (spec §5.2).
OPENALEX_KEPT_TYPES = frozenset(
    {"article", "preprint", "review", "conference-paper", "book", "book-chapter"}
)
#: ORCID work ``type`` values kept (spec §5.2).
ORCID_KEPT_TYPES = frozenset(
    {"journal-article", "conference-paper", "preprint", "book", "book-chapter"}
)
#: publications.doi and publications.journal are varchar(255).
_COLUMN_WIDTH = 255
#: A stored title is one line of at most this many characters: whoever registered a DOI
#: wrote it, and it reaches the persona and the synthesis prompt (security review
#: 2026-10-09).
_TITLE_MAX = 500
#: At most this many OpenAlex lookups per resolve, newest ORCID works first (security and
#: semantic review 2026-10-09). An ORCID-listed work past the cap is kept from ORCID's own
#: metadata, as one OpenAlex does not know is.
MAX_DOI_LOOKUPS = 50

Lookup = Callable[[str], Awaitable[dict[str, Any] | None]]


async def collect_non_pubmed_works(
    result: CorpusResult, *, lookup: Lookup | None = None,
    enriched_dois: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The storable non-PubMed works of one resolve, newest first. Each record is
    ``{"doi", "title", "abstract", "journal", "year", "provenance"}``: ``doi`` in its source's
    spelling (ORCID's when the PI lists the work), ``provenance`` "s1" (ORCID-listed), "s1,s2"
    (ORCID-listed and in OpenAlex's list) or "openalex" (OpenAlex only).

    Skipped: works with a PMID or a DOI PubMed has (``result.pubmed_dois``), works with no DOI
    or no title, retracted works, and works of no kept type (the OpenAlex type decides when
    OpenAlex has one, else the ORCID type: ORCID users label errata as journal articles).
    One OpenAlex singleton lookup (free) per ORCID-listed DOI the S2 list lacks, through
    ``lookup`` (default ``fetch_work_by_doi``), newest first and at most MAX_DOI_LOOKUPS; a
    lookup failure other than "no such DOI" RAISES. ``enriched_dois`` (folded) are the PI's
    PMID-less rows that already carry an abstract: they are not looked up again, and an
    ORCID-listed one the S2 list lacks yields no record, so the cap goes to works not yet
    enriched and ORCID's text never overwrites OpenAlex's."""
    lookup = lookup or fetch_work_by_doi
    s2 = _by_doi(result.openalex_works, result.pubmed_dois)
    orcid = _by_doi(result.orcid_works, result.pubmed_dois)
    looked_up: dict[str, dict[str, Any] | None] = {}
    missing = sorted(
        (fold for fold in orcid if fold not in s2 and fold not in enriched_dois),
        key=lambda fold: (-(orcid[fold].get("year") or 0), fold),
    )
    for fold in missing[:MAX_DOI_LOOKUPS]:
        looked_up[fold] = await lookup(orcid[fold]["doi"])
    records = []
    for fold in sorted({*s2, *orcid}):
        if fold in enriched_dois and fold not in s2:
            continue  # stored with an abstract; nothing new without a lookup
        record = _record(orcid.get(fold), s2.get(fold), looked_up.get(fold))
        if record is not None:
            records.append(record)
    return _newest_first(_one_per_title(records))


def _by_doi(
    works: Iterable[dict[str, Any]], pubmed_dois: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Works with a DOI and no PMID whose DOI PubMed does not have, by folded DOI (the first
    of a repeated DOI wins), each with its DOI normalised."""
    out: dict[str, dict[str, Any]] = {}
    for work in works:
        doi = normalize_doi(work.get("doi"))
        if not doi or work.get("pmid") or len(doi) > _COLUMN_WIDTH:
            continue
        fold = _doi_fold(doi)
        if fold not in pubmed_dois:
            out.setdefault(fold, {**work, "doi": doi})
    return out


def _first(*values: Any) -> Any:
    """The first value that is neither None nor blank text, stripped when it is text."""
    for value in values:
        if isinstance(value, str):
            if value.strip():
                return value.strip()
        elif value is not None:
            return value
    return None


def _one_line(limit: int, *values: Any) -> str | None:
    """The first non-blank text value collapsed to single spaces and cut to ``limit``."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:limit]
    return None


def _record(
    orcid_work: dict[str, Any] | None, s2_work: dict[str, Any] | None,
    looked_up: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """One storable record, or None when the work must be skipped."""
    oa = s2_work or looked_up or {}
    if oa.get("pmid") or oa.get("is_retracted"):
        return None  # PubMed has it after all, or it was retracted
    listed = orcid_work or {}
    oa_type = oa.get("type")
    kept = (oa_type in OPENALEX_KEPT_TYPES) if oa_type else (
        listed.get("type") in ORCID_KEPT_TYPES
    )
    if not kept:
        return None
    title = _one_line(_TITLE_MAX, oa.get("title"), listed.get("title"))
    if title is None:
        return None
    return {
        "doi": (orcid_work or s2_work or {}).get("doi") or oa.get("doi"),
        "title": title,
        "abstract": _first(oa.get("abstract")),
        "journal": _one_line(_COLUMN_WIDTH, oa.get("venue"), listed.get("journal")),
        "year": _first(oa.get("year"), listed.get("year")),
        "provenance": _provenance(orcid_work is not None, s2_work is not None),
    }


def _provenance(in_orcid: bool, in_s2: bool) -> str:
    if in_orcid:
        return "s1,s2" if in_s2 else "s1"
    return PROVENANCE_OPENALEX


def _rank(rec: dict[str, Any]) -> tuple:
    """Smaller is better: the later year, then an ORCID-listed record, then the lower DOI."""
    return (-(rec.get("year") or 0), rec["provenance"] == PROVENANCE_OPENALEX,
            _doi_fold(rec["doi"]))


def _one_per_title(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per normalised title (spec §5.2): the published version over its preprint."""
    best: dict[str, dict[str, Any]] = {}
    for rec in records:
        key = _normalize_title(rec["title"]) or f"doi:{_doi_fold(rec['doi'])}"
        held = best.get(key)
        if held is None or _rank(rec) < _rank(held):
            best[key] = rec
    return list(best.values())


def _newest_first(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: (-(r.get("year") or 0), _doi_fold(r["doi"])))
