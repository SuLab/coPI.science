"""The OpenAlex works source: Baltimore-lineage articles, preprints and reviews by
publication date (spec 2026-10-30 §6.1), each mapped to a `DocumentRecord` with its §5.2
qualification already applied.
"""
from __future__ import annotations

from datetime import date

from src.landscaping.institutions import qualifies_openalex
from src.landscaping.sources import DocumentRecord, SliceResult
from src.services import openalex
from src.services.identifiers import normalize_doi, normalize_orcid, normalize_pmid

#: The §5.1 lineage roots: JHU, JH Medicine, Kennedy Krieger, JH Children's Center, JH Center
#: for Health Security and Jhpiego. Lineage reaches their child institutions too; the
#: §5.2 qualification then tests each authorship against the full Baltimore set.
LINEAGE_FILTER = (
    "authorships.institutions.lineage:"
    "I145311948|I2799853436|I1298154867|I4210129832|I4210114877|I4405267562"
)
_TYPE_FILTER = "type:article|preprint|review"
#: The §6.1 select list.
SELECT = (
    "id,ids,doi,title,type,publication_date,language,is_retracted,authorships,"
    "abstract_inverted_index,primary_location,best_oa_location,open_access,primary_topic,"
    "referenced_works"
)
#: A list response caps `authorships` at this many; a work listed with exactly this many is
#: fetched again singly before qualification (D39).
_LIST_AUTHORSHIP_CAP = 100


def _bare_id(url: object) -> str | None:
    """An OpenAlex entity id without its ``https://openalex.org/`` prefix."""
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip().rsplit("/", 1)[-1]


def _pmcid(value: object) -> str | None:
    """``PMC<digits>`` from OpenAlex's PMC article URL or a bare id; None otherwise."""
    if not isinstance(value, str):
        return None
    tail = value.strip().rstrip("/").rsplit("/", 1)[-1].upper()
    digits = tail[3:] if tail.startswith("PMC") else tail
    return f"PMC{digits}" if digits.isdigit() else None


def _ids(work: dict) -> dict[str, str]:
    raw = work.get("ids") or {}
    candidates = {
        "doi": normalize_doi(raw.get("doi") or work.get("doi")),
        "pmid": normalize_pmid(raw.get("pmid")),
        "pmcid": _pmcid(raw.get("pmcid")),
        "openalex": _bare_id(work.get("id") or raw.get("openalex")),
    }
    return {k: v for k, v in candidates.items() if v}


def _published(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _license(work: dict) -> str | None:
    """The open-access copy's license, else the primary location's."""
    for key in ("best_oa_location", "primary_location"):
        lic = (work.get(key) or {}).get("license")
        if isinstance(lic, str) and lic:
            return lic
    return None


def _senior(authorship: dict) -> dict:
    """The qualifying authorship as a §5.2 senior name, for the lab key."""
    author = authorship.get("author") or {}
    name = author.get("display_name") or authorship.get("raw_author_name") or ""
    orcid = author.get("orcid")
    return {
        "name": " ".join(str(name).split()),
        "orcid": normalize_orcid(orcid) if isinstance(orcid, str) else None,
        "openalex_author_id": _bare_id(author.get("id")),
        "nih_pi_profile_id": None,
    }


def _extra(work: dict) -> dict:
    """What intake keeps as it is: venue, open-access status and copy, topic, references."""
    source = (work.get("primary_location") or {}).get("source") or {}
    topic = work.get("primary_topic") or {}
    return {
        "venue": source.get("display_name"),
        "open_access": work.get("open_access"),
        "best_oa_location": work.get("best_oa_location"),
        "primary_topic": topic.get("display_name") if isinstance(topic, dict) else None,
        "referenced_works": [
            ref for ref in (_bare_id(r) for r in work.get("referenced_works") or []) if ref
        ],
    }


def to_document(work: dict) -> DocumentRecord:
    """One OpenAlex work (the `SELECT` fields) as a `DocumentRecord`.

    `role_basis` is ``"corresponding"`` or ``"last"`` when the work qualifies (§5.2,
    `qualifies_openalex`), else None. `authorships` then holds the single qualifying
    authorship, `senior_names` its author, and `raw_qualifier` its Baltimore-test inputs
    (bare institution ids and raw affiliation strings); all three are empty when the work
    does not qualify. `summary` is the reconstructed abstract (`summary_source`
    ``"openalex"`` when there is one)."""
    _, role_basis, authorship = qualifies_openalex(work)
    summary = openalex.reconstruct_abstract(work.get("abstract_inverted_index"))
    doc_type = work.get("type") or ""
    raw_qualifier: dict = {}
    if authorship is not None:
        raw_qualifier = {
            "institution_ids": [
                i for i in (_bare_id((inst or {}).get("id"))
                            for inst in authorship.get("institutions") or []) if i
            ],
            "raw_affiliations": list(authorship.get("raw_affiliation_strings") or []),
        }
    return DocumentRecord(
        kind="preprint" if doc_type == "preprint" else "work",
        ids=_ids(work),
        title=" ".join(str(work.get("title") or "").split()),
        summary=summary,
        published=_published(work.get("publication_date")),
        doc_type=doc_type,
        language=work.get("language"),
        license=_license(work),
        authorships=[authorship] if authorship is not None else None,
        role_basis=role_basis,
        raw_qualifier=raw_qualifier,
        summary_source="openalex" if summary else None,
        retracted=work.get("is_retracted") is True,
        senior_names=[_senior(authorship)] if authorship is not None else [],
        extra=_extra(work),
    )


async def _complete(work: dict) -> dict:
    """The work with its full author list: refetched singly (free) when the list response
    may have cut it at the cap (D39); the listed copy when the refetch finds nothing."""
    if len(work.get("authorships") or []) != _LIST_AUTHORSHIP_CAP:
        return work
    full = await openalex.fetch_work(work.get("id") or "", SELECT)
    return full or work


async def fetch_slice(date_from: date, date_to: date) -> SliceResult:
    """Every Baltimore-lineage article, preprint and review published from `date_from` to
    `date_to` inclusive, each mapped by `to_document` (qualifying or not; S1 decides). Pages
    through `openalex.iter_works` with no cap: `expected` is OpenAlex's reported total and
    `received` the rows listed, for the §12.4 completeness check. RAISES on a fetch failure;
    may raise `JobDeferred` from the request budget."""
    filter_ = (
        f"{LINEAGE_FILTER},{_TYPE_FILTER},"
        f"from_publication_date:{date_from.isoformat()},"
        f"to_publication_date:{date_to.isoformat()}"
    )
    rows: list[DocumentRecord] = []
    expected = 0
    received = 0
    async for page, count in openalex.iter_works(filter_, SELECT):
        expected = count
        received += len(page)
        for work in page:
            rows.append(to_document(await _complete(work)))
    return SliceResult(rows=rows, expected=expected, received=received)
