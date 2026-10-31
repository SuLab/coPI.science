"""The GEO datasets source: GEO series (`gse[ETYP]`) submitted under a §5.1 institution
name variant (`[INST]`), by public-release date (spec 2026-10-30 §6.1).

Datasets are evidence only (D3): every record is ``kind="dataset"`` with no `role_basis`,
and intake attaches it to a stored work through the PMIDs in its `related_ids`.

Requests go through `pubmed._ncbi_get`, because the NCBI key and the process-wide NCBI
pacer (`pubmed.set_min_interval`, D58) live only in `src/services/pubmed.py`.
"""
from __future__ import annotations

from datetime import date, datetime

from src.landscaping.sources import DocumentRecord, SliceResult
from src.services import pubmed
from src.services.identifiers import normalize_pmid

#: GEO `[INST]` name variants of the §5.1 set.
INST_VARIANTS = (
    "Johns Hopkins University",
    "Johns Hopkins School of Medicine",
    "Johns Hopkins University School of Medicine",
    "Johns Hopkins Medicine",
    "Johns Hopkins Bloomberg School of Public Health",
    "Johns Hopkins Hospital",
    "Kennedy Krieger Institute",
)
PAGE = 500


def term(date_from: date, date_to: date) -> str:
    """The GDS ESearch term: any `INST_VARIANTS` name, series only, released in the range
    (inclusive)."""
    inst = " OR ".join(f'"{n}"[INST]' for n in INST_VARIANTS)
    return (f"({inst}) AND gse[ETYP] AND "
            f'("{date_from:%Y/%m/%d}"[PDAT] : "{date_to:%Y/%m/%d}"[PDAT])')


def _pdat(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y/%m/%d").date()
    except ValueError:
        return None


def to_document(summary: dict) -> DocumentRecord:
    """One GDS ESummary record as an evidence-only dataset record (D3): no `role_basis`,
    `related_ids["pmid"]` the series' linked PubMed ids."""
    accession = summary.get("accession")
    text = " ".join(str(summary.get("summary") or "").split()) or None
    pmids = [p for p in (normalize_pmid(x) for x in summary.get("pubmedids") or []) if p]
    return DocumentRecord(
        kind="dataset",
        ids={"geo_accession": str(accession)} if accession else {},
        title=" ".join(str(summary.get("title") or "").split()),
        summary=text,
        published=_pdat(summary.get("pdat")),
        doc_type="dataset",
        language=None,
        license=None,
        authorships=None,
        role_basis=None,
        summary_source="geo" if text else None,
        related_ids={"pmid": pmids} if pmids else {},
        extra={
            "gds_type": summary.get("gdstype"),
            "taxon": summary.get("taxon"),
            "platform": summary.get("gpl"),
            "n_samples": summary.get("n_samples"),
        },
    )


async def _search(query: str, retstart: int) -> tuple[int, list[str]]:
    resp = await pubmed._ncbi_get(f"{pubmed.EUTILS_BASE}/esearch.fcgi", {
        "db": "gds", "term": query, "retmode": "json", "retstart": retstart, "retmax": PAGE,
    })
    result = resp.json().get("esearchresult") or {}
    return int(result.get("count") or 0), [str(i) for i in result.get("idlist") or []]


async def _summaries(uids: list[str]) -> list[dict]:
    resp = await pubmed._ncbi_get(f"{pubmed.EUTILS_BASE}/esummary.fcgi", {
        "db": "gds", "id": ",".join(uids), "retmode": "json",
    })
    result = resp.json().get("result") or {}
    return [result[u] for u in result.get("uids") or [] if isinstance(result.get(u), dict)]


async def fetch_geo(date_from: date, date_to: date) -> SliceResult:
    """Every matching GEO series released from `date_from` to `date_to` inclusive, `PAGE`
    ids a request. `expected` is ESearch's count and `received` the summaries ESummary
    returned. RAISES on an HTTP failure."""
    query = term(date_from, date_to)
    rows: list[DocumentRecord] = []
    expected = 0
    received = 0
    retstart = 0
    while True:
        expected, uids = await _search(query, retstart)
        if not uids:
            break
        summaries = await _summaries(uids)
        rows.extend(to_document(s) for s in summaries)
        received += len(summaries)
        retstart += PAGE
        if retstart >= expected:
            break
    return SliceResult(rows=rows, expected=expected, received=received)
