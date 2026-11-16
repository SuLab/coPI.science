"""The DataCite datasets source: datasets whose creators are affiliated with a §5.1 ROR id
or name variant, by creation date (spec 2026-10-30 §6.1).

Datasets are evidence only (D3): every record is ``kind="dataset"`` with no `role_basis`,
and intake attaches it to a stored work through the DOIs and PMIDs in its `related_ids`.
"""
from __future__ import annotations

from datetime import date

import httpx

from src.landscaping.institutions import ROR_IDS
from src.landscaping.sources import DocumentRecord, SliceResult
from src.services.http_pacing import TRANSIENT_STATUSES, with_retries
from src.services.identifiers import normalize_doi, normalize_pmid

DOIS_URL = "https://api.datacite.org/dois"
PAGE_SIZE = 1000
#: Affiliation names that carry no ROR id on older records.
NAME_VARIANTS = (
    "Johns Hopkins University",
    "Johns Hopkins Medicine",
    "Johns Hopkins University School of Medicine",
    "Johns Hopkins Bloomberg School of Public Health",
    "Kennedy Krieger Institute",
)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


def _query(date_from: date, date_to: date) -> str:
    """The DataCite search: a §5.1 ROR affiliation id or name variant, created in the
    range (inclusive)."""
    rors = [f'creators.affiliation.affiliationIdentifier:"https://ror.org/{r}"'
            for r in sorted(ROR_IDS)]
    names = [f'creators.affiliation.name:"{n}"' for n in NAME_VARIANTS]
    return (f"({' OR '.join(rors + names)}) AND "
            f"created:[{date_from.isoformat()} TO {date_to.isoformat()}]")


def _day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _related(attrs: dict) -> dict[str, list[str]]:
    """The DOIs and PMIDs the dataset names, whatever the relation (D3)."""
    out: dict[str, list[str]] = {}
    for rel in attrs.get("relatedIdentifiers") or []:
        if not isinstance(rel, dict):
            continue
        kind = str(rel.get("relatedIdentifierType") or "").upper()
        value = rel.get("relatedIdentifier")
        norm = normalize_doi(value) if kind == "DOI" else (
            normalize_pmid(value) if kind == "PMID" else None)
        if norm and norm not in out.setdefault(kind.lower(), []):
            out[kind.lower()].append(norm)
    return {k: v for k, v in out.items() if v}


def _summary(attrs: dict) -> str | None:
    descs = [d for d in attrs.get("descriptions") or [] if isinstance(d, dict)
             and d.get("description")]
    best = next((d for d in descs if d.get("descriptionType") == "Abstract"),
                descs[0] if descs else None)
    return " ".join(str(best["description"]).split()) if best else None


def _published(attrs: dict) -> date | None:
    for d in attrs.get("dates") or []:
        if isinstance(d, dict) and d.get("dateType") == "Issued" and _day(d.get("date")):
            return _day(d.get("date"))
    return _day(attrs.get("created"))


def to_document(record: dict) -> DocumentRecord:
    """One DataCite `data` item as an evidence-only dataset record (D3): no `role_basis`,
    `related_ids` its named DOIs and PMIDs."""
    attrs = record.get("attributes") or {}
    doi = normalize_doi(attrs.get("doi") or record.get("id"))
    titles = [t.get("title") for t in attrs.get("titles") or [] if isinstance(t, dict)]
    rights = [r for r in attrs.get("rightsList") or [] if isinstance(r, dict)]
    summary = _summary(attrs)
    creators = [c for c in attrs.get("creators") or [] if isinstance(c, dict)]
    return DocumentRecord(
        kind="dataset",
        ids={"datacite_doi": doi} if doi else {},
        title=" ".join(str(next((t for t in titles if t), "")).split()),
        summary=summary,
        published=_published(attrs),
        doc_type="dataset",
        language=attrs.get("language"),
        license=(rights[0].get("rightsIdentifier") or rights[0].get("rightsUri")) if rights else None,
        authorships=None,
        role_basis=None,
        summary_source="datacite" if summary else None,
        related_ids=_related(attrs),
        extra={
            "publisher": attrs.get("publisher"),
            "url": attrs.get("url"),
            "creators": [{"name": c.get("name"),
                          "affiliations": [a.get("name") if isinstance(a, dict) else a
                                           for a in c.get("affiliation") or []]}
                         for c in creators],
            "relations": [{"id": r.get("relatedIdentifier"),
                           "type": r.get("relatedIdentifierType"),
                           "relation": r.get("relationType")}
                          for r in attrs.get("relatedIdentifiers") or [] if isinstance(r, dict)],
        },
    )


async def fetch_datacite(date_from: date, date_to: date) -> SliceResult:
    """Every matching dataset created from `date_from` to `date_to` inclusive, cursor-paged
    `PAGE_SIZE` at a time until no `links.next`. `expected` is DataCite's `meta.total`.
    RAISES on an HTTP error after retries."""
    params: dict | None = {
        "query": _query(date_from, date_to),
        "resource-type-id": "dataset",
        "affiliation": "true",
        "page[size]": PAGE_SIZE,
        "page[cursor]": 1,
    }
    url = DOIS_URL
    rows: list[DocumentRecord] = []
    total: int | None = None
    async with _make_client() as client:
        while url:
            resp = await with_retries(
                lambda u=url, p=params: client.get(u, params=p),
                attempts=3, backoff=lambda a: 2.0 * (a + 1), retry_statuses=TRANSIENT_STATUSES,
            )
            resp.raise_for_status()
            data = resp.json()
            if total is None and isinstance((data.get("meta") or {}).get("total"), int):
                total = data["meta"]["total"]
            page = data.get("data") or []
            rows.extend(to_document(r) for r in page)
            # `links.next` carries the whole query and the next cursor.
            url = (data.get("links") or {}).get("next") if page else None
            params = None
    return SliceResult(rows=rows, expected=len(rows) if total is None else total,
                       received=len(rows))
