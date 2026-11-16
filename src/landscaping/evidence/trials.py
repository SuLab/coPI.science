"""ClinicalTrials.gov API v2 trial counts by phase for a condition × intervention (spec §6.3,
§7.4 step 5). Requests share `project_search`'s process-wide 1 req/s ClinicalTrials.gov pacer,
so the research tools and landscaping never add up past it."""
from __future__ import annotations

from collections import Counter

import httpx

from src.landscaping import evidence
from src.services.project_search import _TRIALS_PACER, CTGOV_STUDIES_URL

PAGE_SIZE = 1000
#: The v2 fields a count needs (nested ``protocolSection`` shape, as `project_search` reads).
FIELDS = "NCTId,Phase,OverallStatus"
#: The phase key of a study that lists none (observational studies, for one).
NO_PHASE = "NA"


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _phase_key(study: dict) -> str:
    """``PHASE2``, or ``PHASE1/PHASE2`` for a combined-phase study, or `NO_PHASE`."""
    phases = (((study.get("protocolSection") or {}).get("designModule") or {})
              .get("phases")) or []
    return "/".join(phases) if phases else NO_PHASE


def _status(study: dict) -> str:
    return (((study.get("protocolSection") or {}).get("statusModule") or {})
            .get("overallStatus")) or "UNKNOWN"


async def trials_count(condition: str, intervention: str) -> dict:
    """``{"total", "by_phase", "by_status"}`` over every study ClinicalTrials.gov returns
    for the condition and intervention queries; ``total`` is the API's own count."""
    params = {"query.cond": condition, "query.intr": intervention, "countTotal": "true",
              "pageSize": PAGE_SIZE, "fields": FIELDS}
    by_phase: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    total: int | None = None
    token: str | None = None
    while True:
        page_params = {**params, "pageToken": token} if token else params
        body = await evidence.request_json(_make_client, CTGOV_STUDIES_URL,
                                           pacer=_TRIALS_PACER, params=page_params)
        if total is None:
            total = int(body.get("totalCount") or 0)
        for study in body.get("studies") or []:
            by_phase[_phase_key(study)] += 1
            by_status[_status(study)] += 1
        token = body.get("nextPageToken")
        if not token:
            break
    return {"total": total or 0, "by_phase": dict(by_phase), "by_status": dict(by_status)}
