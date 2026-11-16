"""The USPTO Open Data Portal patents source: applications whose applicant or assignee is
Johns Hopkins or Kennedy Krieger, by calendar quarter (spec 2026-10-30 §6.1), mapped to
`DocumentRecord`s.

It shares the ODP transport and the process-wide pacer of `src/services/odp.py`. The query
is `uspto_inventor.inventor_query`'s applicant clause without the inventor clause. ODP's
field names for dates, abstract, claims and inventor cities are unverified until the
Phase 0 probe (spec §15), so the parser takes each from where it may be, and a slice logs
the fields its records lacked.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

import httpx

from src.config import get_settings
from src.landscaping.sources import DocumentRecord, SliceResult
from src.services.http_pacing import with_retries
from src.services.odp import ODP_PACER, ODP_SEARCH_URL, odp_client, odp_headers

logger = logging.getLogger(__name__)

_NAMES = ("Johns Hopkins", "Kennedy Krieger")
_NAME_FIELDS = (
    "applicationMetaData.applicantBag.applicantNameText",
    "assignmentBag.assigneeBag.assigneeNameText",
)
#: Applicant or assignee is one of `_NAMES` (§5.2 "Patent").
QUERY = " OR ".join(f'{field}:"{name}"' for field in _NAME_FIELDS for name in _NAMES)
#: The date a quarter slices on: publication, the §5.3 window's date for patents.
DATE_FIELD = "applicationMetaData.earliestPublicationDate"
PAGE_LIMIT = 100
#: Inventor cities that, when they are every inventor's, mark an Applied Physics
#: Laboratory invention (§6.1 "APL patents").
APL_CITIES = frozenset({"LAUREL", "NORTH LAUREL"})


def _api_key() -> str:
    """The ODP key, read as `src/services/patents.py` reads it."""
    s = get_settings()
    return s.uspto_api_key or s.patentsview_api_key


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return odp_client(timeout=60, follow_redirects=False)


def _quarter(year: int, quarter: int) -> tuple[date, date]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter!r}")
    start = date(year, 3 * quarter - 2, 1)
    nxt = date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)
    return start, nxt - timedelta(days=1)


def _day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _names(items: object, key: str) -> list[str]:
    return [str(i[key]).strip() for i in items or [] if isinstance(i, dict) and i.get(key)]


def _inventors(meta: dict) -> list[dict]:
    """Each inventor's name and city (None when ODP gives none)."""
    out = []
    for inv in meta.get("inventorBag") or []:
        if not isinstance(inv, dict):
            continue
        name = inv.get("inventorNameText") or " ".join(
            str(inv.get(k) or "") for k in ("firstName", "lastName"))
        addresses = inv.get("correspondenceAddressBag") or []
        city = next((a.get("cityName") for a in addresses
                     if isinstance(a, dict) and a.get("cityName")), None)
        out.append({"name": " ".join(str(name).split()),
                    "city": str(city).strip() if city else None})
    return out


def _is_apl(inventors: list[dict]) -> bool:
    """Every inventor has a city and every city is Laurel or North Laurel."""
    return bool(inventors) and all(
        inv["city"] and inv["city"].upper() in APL_CITIES for inv in inventors)


def _assignees(app: dict) -> list[str]:
    out: list[str] = []
    for bag in app.get("assignmentBag") or []:
        if isinstance(bag, dict):
            out += _names(bag.get("assigneeBag"), "assigneeNameText")
    return out


def to_document(app: dict) -> DocumentRecord:
    """One ODP application record as a patent record.

    `role_basis` is ``"assignee"`` when an applicant or assignee names Johns Hopkins or
    Kennedy Krieger, else None. `senior_names` holds the first inventor. `raw_qualifier`
    carries the applicant and assignee names, the inventors' cities, and `apl`: True only
    when every inventor has a city and all are Laurel or North Laurel, which S1 excludes
    as ``excluded:apl`` (§6.1). `extra["missing_fields"]` names the expected fields this
    record lacked."""
    meta = app.get("applicationMetaData") or {}
    missing = [f for f in ("inventionTitle", "filingDate", "earliestPublicationDate",
                           "inventorBag", "applicantBag") if not meta.get(f)]
    abstract = app.get("abstractText") or meta.get("abstractText") or None
    if not abstract:
        missing.append("abstractText")
    applicants = _names(meta.get("applicantBag"), "applicantNameText")
    assignees = _assignees(app)
    inventors = _inventors(meta)
    first_inventor = meta.get("firstInventorName") or (inventors[0]["name"] if inventors else "")
    qualifies = any(n.lower() in name.lower() for name in applicants + assignees for n in _NAMES)
    number = app.get("applicationNumberText")
    return DocumentRecord(
        kind="patent",
        ids={"patent_application": str(number)} if number else {},
        title=" ".join(str(meta.get("inventionTitle") or "").split()),
        summary=abstract,
        published=_day(meta.get("earliestPublicationDate")) or _day(meta.get("filingDate")),
        doc_type="patent_grant" if meta.get("patentNumber") else "patent_application",
        language=None,
        license=None,
        authorships=None,
        role_basis="assignee" if qualifies else None,
        raw_qualifier={
            "applicants": applicants,
            "assignees": assignees,
            "inventor_cities": [inv["city"] for inv in inventors if inv["city"]],
            "apl": _is_apl(inventors),
        },
        summary_source="odp" if abstract else None,
        senior_names=[{"name": " ".join(str(first_inventor).split()), "orcid": None,
                       "openalex_author_id": None, "nih_pi_profile_id": None}]
        if first_inventor else [],
        extra={
            "filing_date": meta.get("filingDate"),
            "publication_number": meta.get("earliestPublicationNumber"),
            "publication_date": meta.get("earliestPublicationDate"),
            "patent_number": meta.get("patentNumber"),
            "grant_date": meta.get("grantDate"),
            "status": meta.get("applicationStatusDescriptionText"),
            "inventors": inventors,
            "claims": app.get("claims") or meta.get("claims"),
            "missing_fields": missing,
        },
    )


async def fetch_quarter(y: int, q: int) -> SliceResult:
    """Every qualifying application published in quarter `q` (1-4) of year `y`,
    `PAGE_LIMIT` a request by offset until a short page, the reported `count`, or a 404
    (ODP's answer for no matches). `expected` is ODP's `count` (the records received when
    no response carries one, logged). RAISES `RuntimeError`
    without a key, and on any other error status after retries."""
    key = _api_key()
    if not key:
        raise RuntimeError("ODP: USPTO_API_KEY is not set")
    start, end = _quarter(y, q)
    rows: list[DocumentRecord] = []
    total: int | None = None
    offset = 0
    async with _make_client() as client:
        while True:
            body = {
                "q": QUERY,
                "rangeFilters": [{"field": DATE_FIELD, "valueFrom": start.isoformat(),
                                  "valueTo": end.isoformat()}],
                "pagination": {"offset": offset, "limit": PAGE_LIMIT},
                "sort": [{"field": DATE_FIELD, "order": "asc"}],
            }
            resp = await with_retries(
                lambda b=body: client.post(ODP_SEARCH_URL, json=b, headers=odp_headers(key)),
                attempts=3, backoff=lambda a: 2.0 ** a,
                retry_statuses=frozenset({429}), pacer=ODP_PACER,
            )
            if resp.status_code == 404:
                total = len(rows) if total is None else total
                break
            resp.raise_for_status()
            data = resp.json()
            bag = data.get("patentFileWrapperDataBag") or []
            if isinstance(data.get("count"), int):
                total = data["count"]
            rows.extend(to_document(app) for app in bag)
            offset += PAGE_LIMIT
            if len(bag) < PAGE_LIMIT or (total is not None and offset >= total):
                break
    lacking = Counter(f for r in rows for f in r.extra["missing_fields"])
    if lacking:
        logger.warning("[landscape.odp] %d-Q%d: records lacking fields %s", y, q,
                       dict(lacking))
    if total is None:
        logger.warning("[landscape.odp] %d-Q%d: response carried no count; expected set to "
                       "the %d records received", y, q, len(rows))
        total = len(rows)
    return SliceResult(rows=rows, expected=total, received=len(rows))
