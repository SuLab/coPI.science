"""The PubMed works source (spec §6.1): Baltimore Hopkins affiliations by ESearch.

Backfill runs one `[dp]` month per slice and the daily incremental one `[edat]` range. ESearch
serves at most 9,999 records per query (K8), so a slice at or above that is refused, never
truncated. Every NCBI call goes through `src.services.pubmed`, which holds the key and the
process-wide pacing (D58).
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from src.landscaping.institutions import is_baltimore_affiliation
from src.landscaping.sources import DocumentRecord, SliceResult
from src.services import pubmed
from src.services.identifiers import normalize_doi

AFFILIATION_TERM = '("Johns Hopkins"[ad] OR "Kennedy Krieger"[ad] OR "Jhpiego"[ad])'

#: ESearch's per-query ceiling (K8): a slice whose count reaches it cannot be paged in full.
ESEARCH_LIMIT = 9_999
_PAGE_SIZE = 1_000

# PubMed publication types mapped to the OpenAlex type vocabulary S1 tests (spec §5.3), in
# precedence order: an excluded type wins over "Review", and "Review" over a plain article.
_DOC_TYPES = (
    ("Published Erratum", "erratum"),
    ("Retraction of Publication", "retraction"),
    ("Editorial", "editorial"),
    ("Letter", "letter"),
    ("Comment", "comment"),
    ("Congress", "conference"),
    ("Preprint", "preprint"),
    ("Systematic Review", "review"),
    ("Review", "review"),
)


async def fetch_month(year: int, month: int) -> SliceResult:
    """One backfill slice: every record whose `[dp]` falls in ``year``-``month``."""
    last_day = calendar.monthrange(year, month)[1]
    return await _fetch_term(
        _date_range(date(year, month, 1), date(year, month, last_day), "dp")
    )


async def fetch_edat(day_from: date, day_to: date) -> SliceResult:
    """The daily incremental slice: records entered into PubMed (`[edat]`) from
    ``day_from`` to ``day_to``, both inclusive."""
    return await _fetch_term(_date_range(day_from, day_to, "edat"))


def _date_range(start: date, end: date, field: str) -> str:
    return (
        f'{AFFILIATION_TERM} AND ("{start:%Y/%m/%d}"[{field}] : "{end:%Y/%m/%d}"[{field}])'
    )


async def _fetch_term(term: str) -> SliceResult:
    """Page ``term`` through ESearch, fetch its records and map them. ``expected`` is
    ESearch's count and ``received`` the records mapped, so a record EFetch did not return
    shows as a shortfall (spec §12.4).

    RAISES ``ValueError`` when the count reaches ``ESEARCH_LIMIT``: the slice must be cut
    smaller rather than stored incomplete.
    """
    count, page = await pubmed.esearch_page(term, retstart=0, retmax=_PAGE_SIZE)
    if count >= ESEARCH_LIMIT:
        raise ValueError(
            f"PubMed slice has {count} records, at or above ESearch's {ESEARCH_LIMIT}; "
            "cut it smaller"
        )
    pages = [page]
    start = len(page)
    while len(page) == _PAGE_SIZE and start < count:
        _, page = await pubmed.esearch_page(term, retstart=start, retmax=_PAGE_SIZE)
        pages.append(page)
        start += len(page)
    # A record entered between two pages can shift one into the next; keep it once.
    pmids = list(dict.fromkeys(pmid for chunk in pages for pmid in chunk))
    rows: list[DocumentRecord] = []
    # One EFetch batch at a time keeps a month's XML out of memory all at once.
    for i in range(0, len(pmids), pubmed.EFETCH_BATCH_SIZE):
        xml_text = await pubmed.fetch_pubmed_xml(pmids[i : i + pubmed.EFETCH_BATCH_SIZE])
        extras = pubmed.parse_pubmed_extras(xml_text)
        for record in pubmed._parse_pubmed_xml(xml_text):
            pmid = str(record.get("pmid") or "")
            if pmid in extras:
                rows.append(to_document(record, extras[pmid]))
    return SliceResult(rows=rows, expected=count, received=len(rows))


def _doc_type(publication_types: list[str]) -> str:
    present = set(publication_types)
    for pub_type, doc_type in _DOC_TYPES:
        if pub_type in present:
            return doc_type
    return "article"


def _qualify(last_author: dict | None) -> tuple[str | None, dict[str, Any]]:
    """(role_basis, raw_qualifier) for a PubMed-only work (spec §5.2): it qualifies when the
    last valid individual author has an affiliation passing the §5.1 matcher. PubMed has no
    corresponding-author flag, so no other author is considered."""
    if last_author is None:
        return None, {"last_author": None, "affiliation": None}
    matched = next(
        (aff for aff in last_author.get("affiliations") or [] if is_baltimore_affiliation(aff)),
        None,
    )
    qualifier = {"last_author": last_author, "affiliation": matched}
    return ("pubmed_last_author" if matched else None), qualifier


def _senior_names(last_author: dict | None) -> list[dict]:
    """The lab-key person of a PubMed-only work (spec §5.2): its last valid individual
    author."""
    if last_author is None:
        return []
    name = " ".join(p for p in (last_author.get("fore"), last_author.get("last")) if p)
    return [{
        "name": name,
        "orcid": last_author.get("orcid"),
        "openalex_author_id": None,
        "nih_pi_profile_id": None,
    }]


def to_document(record: dict, extras: dict) -> DocumentRecord:
    """Map one ``_parse_pubmed_xml`` record and its ``parse_pubmed_extras`` entry. The
    summary is PubMed's own abstract (spec §6.1 summary precedence); ``role_basis`` is
    ``"pubmed_last_author"`` or None (§5.2)."""
    ids = {"pmid": str(record.get("pmid") or "")}
    if doi := normalize_doi(record.get("doi")):
        ids["doi"] = doi
    if record.get("pmcid"):
        ids["pmcid"] = str(record["pmcid"])
    publication_types = extras.get("publication_types") or record.get("pub_types") or []
    doc_type = _doc_type(publication_types)
    last_author = extras.get("last_author")
    role_basis, qualifier = _qualify(last_author)
    summary = extras.get("abstract") or record.get("abstract") or None
    return DocumentRecord(
        kind="preprint" if doc_type == "preprint" else "work",
        ids=ids,
        title=record.get("title") or "",
        summary=summary,
        published=extras.get("published"),
        doc_type=doc_type,
        language=extras.get("language"),
        license=None,
        authorships=record.get("authors"),
        role_basis=role_basis,
        raw_qualifier=qualifier,
        summary_source="pubmed" if summary else None,
        retracted=bool(extras.get("retracted")),
        senior_names=_senior_names(last_author),
        extra={
            "mesh": extras.get("mesh") or [],
            "keywords": extras.get("keywords") or [],
            "grants": extras.get("grants") or [],
            "publication_types": publication_types,
        },
    )
