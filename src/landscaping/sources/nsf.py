"""The NSF Award API grants source: awards to the §5.1 NSF awardee in an award-date range
(spec 2026-10-30 §6.1), mapped to `DocumentRecord`s.

The API serves at most 25 awards a request (`rpp`), addressed by a 1-based `offset`, and
reports the query's total as `metadata.totalCount`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from src.landscaping.institutions import NSF_AWARDEE
from src.landscaping.sources import DocumentRecord, SliceResult
from src.services.http_pacing import TRANSIENT_STATUSES, Pacer, with_retries

logger = logging.getLogger(__name__)

AWARDS_URL = "https://api.nsf.gov/services/v1/awards.json"
#: The API's page-size ceiling.
RPP = 25
PRINT_FIELDS = (
    "id,title,abstractText,awardeeName,piFirstName,piLastName,pdPIName,coPDPI,date,"
    "startDate,expDate,estimatedTotalAmt,fundsObligatedAmt,fundProgramName,agency"
)
# NSF publishes no rate limit; requests are spaced so a weekly sweep stays polite.
_PACER = Pacer(0.5)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=30)


def _mdy(value: object) -> date | None:
    """A ``MM/DD/YYYY`` NSF date; None when absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _pi_display_name(award: dict) -> str:
    name = " ".join(str(award.get(k) or "") for k in ("piFirstName", "piLastName"))
    return " ".join((name if name.strip() else str(award.get("pdPIName") or "")).split())


def to_document(award: dict) -> DocumentRecord:
    """One NSF award as a grant record. `published` is the award date (else the start
    date); `extra` keeps the period and amounts for the §5.3 grant window. `role_basis` is
    ``"grant_pi"`` when the awardee is the §5.1 NSF awardee, else None."""
    summary = award.get("abstractText") or None
    awardee = " ".join(str(award.get("awardeeName") or "").split())
    pi = _pi_display_name(award)
    start = _mdy(award.get("startDate"))
    end = _mdy(award.get("expDate"))
    return DocumentRecord(
        kind="grant",
        ids={"nsf_award": str(award["id"])} if award.get("id") else {},
        title=" ".join(str(award.get("title") or "").split()),
        summary=summary,
        published=_mdy(award.get("date")) or start,
        doc_type="grant",
        language=None,
        license=None,
        authorships=None,
        role_basis="grant_pi" if awardee.lower() == NSF_AWARDEE.lower() else None,
        raw_qualifier={"awardee": awardee},
        summary_source="nsf" if summary else None,
        senior_names=[{"name": pi, "orcid": None, "openalex_author_id": None,
                       "nih_pi_profile_id": None}] if pi else [],
        extra={
            "project_start": start.isoformat() if start else None,
            "project_end": end.isoformat() if end else None,
            "estimated_total_amount": award.get("estimatedTotalAmt"),
            "funds_obligated_amount": award.get("fundsObligatedAmt"),
            "program": award.get("fundProgramName"),
            "agency": award.get("agency"),
            "co_pis": list(award.get("coPDPI") or []),
        },
    )


async def fetch_nsf(date_from: date, date_to: date) -> SliceResult:
    """Every NSF award to the §5.1 awardee with an award date from `date_from` to `date_to`
    inclusive, `RPP` a request until a short page or the reported total. `expected` is
    `metadata.totalCount` (the rows received when the response carries none, logged).
    RAISES on an HTTP error after retries."""
    params = {
        "awardeeName": f'"{NSF_AWARDEE}"',
        "dateStart": date_from.strftime("%m/%d/%Y"),
        "dateEnd": date_to.strftime("%m/%d/%Y"),
        "printFields": PRINT_FIELDS,
        "rpp": RPP,
    }
    rows: list[DocumentRecord] = []
    total: int | None = None
    offset = 1
    async with _make_client() as client:
        while True:
            page_params = {**params, "offset": offset}
            resp = await with_retries(
                lambda p=page_params: client.get(AWARDS_URL, params=p),
                attempts=3, backoff=lambda a: 2.0 * (a + 1),
                retry_statuses=TRANSIENT_STATUSES, pacer=_PACER,
            )
            resp.raise_for_status()
            body = resp.json().get("response") or {}
            count = (body.get("metadata") or {}).get("totalCount")
            if isinstance(count, int):
                total = count
            awards = body.get("award") or []
            rows.extend(to_document(a) for a in awards)
            offset += RPP
            if len(awards) < RPP or (total is not None and offset > total):
                break
    if total is None:
        logger.warning("[landscape.nsf] response carried no metadata.totalCount; "
                       "expected set to the %d awards received", len(rows))
    return SliceResult(rows=rows, expected=len(rows) if total is None else total,
                       received=len(rows))
