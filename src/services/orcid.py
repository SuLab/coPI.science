"""ORCID API client — fetch profile, grants, and works."""

import logging
from typing import Any

import httpx

from src.services.http_retry import get_with_retry

logger = logging.getLogger(__name__)

ORCID_API_BASE = "https://pub.orcid.org/v3.0"

# Overridable by tests (see test_orcid_contract.py) — the retry loop's own
# exponential backoff, not the ORCID request timeout.
_RETRY_BACKOFF = 0.5


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Null-safe chained dict lookup.

    ORCID emits explicit ``null`` for empty containers (e.g. ``"external-ids": null``), and
    ``dict.get(key, default)`` only substitutes ``default`` when ``key`` is *missing* — not when
    its value is present-but-``None``. This walks ``keys`` through ``d``, treating a ``None``
    (or non-dict) value at any point as "stop, return default" instead of raising on the next
    ``.get()``. See issue #22 COR-15.
    """
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


async def fetch_orcid_record(orcid_id: str) -> dict[str, Any]:
    """Fetch full ORCID record for a given ORCID ID."""
    url = f"{ORCID_API_BASE}/{orcid_id}/record"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await get_with_retry(client, url, headers=headers, backoff=_RETRY_BACKOFF)
        return resp.json()


async def fetch_orcid_profile(orcid_id: str) -> dict[str, Any]:
    """Extract name, affiliation, and email from ORCID record."""
    record = await fetch_orcid_record(orcid_id)
    result: dict[str, Any] = {"orcid": orcid_id}

    # Name
    given = _get(record, "person", "name", "given-names", "value", default="")
    family = _get(record, "person", "name", "family-name", "value", default="")
    result["name"] = f"{given} {family}".strip() or orcid_id

    # Email (first public email)
    emails = _get(record, "person", "emails", "email", default=[])
    for e in emails:
        if e.get("primary") or not result.get("email"):
            result["email"] = e.get("email")

    # Current employment (affiliation) — prefer primary (lowest display-index)
    employments = _get(
        record, "activities-summary", "employments", "affiliation-group", default=[]
    )
    current_employments: list[dict[str, Any]] = []
    for grp in employments:
        for summaries in grp.get("summaries", []):
            emp = summaries.get("employment-summary", {})
            if emp.get("end-date") is None:  # Current employment
                current_employments.append(emp)
                break  # one per group

    def _display_index(emp: dict[str, Any]) -> int:
        try:
            return int(_get(emp, "display-index", default=999))
        except (TypeError, ValueError):
            return 999

    # Sort by display-index ascending: 0 = primary/preferred position
    current_employments.sort(key=_display_index)
    if current_employments:
        emp = current_employments[0]
        result["institution"] = _get(emp, "organization", "name")
        dept = emp.get("department-name")
        if dept:
            result["department"] = dept

    # Researcher URLs (lab website)
    urls = _get(record, "person", "researcher-urls", "researcher-url", default=[])
    for u in urls:
        result["lab_website"] = _get(u, "url", "value")
        break

    return result


async def fetch_orcid_grants(orcid_id: str) -> list[str]:
    """Return list of grant titles from ORCID fundings."""
    url = f"{ORCID_API_BASE}/{orcid_id}/fundings"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await get_with_retry(client, url, headers=headers, backoff=_RETRY_BACKOFF)
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch ORCID grants for %s: %s", orcid_id, exc)
            return []

    titles = []
    for grp in data.get("group", []):
        for summary in grp.get("funding-summary", []):
            title = _get(summary, "title", "title", "value")
            if title:
                titles.append(title)
    return titles


async def fetch_orcid_works(orcid_id: str) -> list[dict[str, Any]]:
    """Return list of works (publications) from ORCID, with PMIDs/DOIs."""
    url = f"{ORCID_API_BASE}/{orcid_id}/works"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await get_with_retry(client, url, headers=headers, backoff=_RETRY_BACKOFF)
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch ORCID works for %s: %s", orcid_id, exc)
            return []

    works = []
    for grp in data.get("group", []):
        for summary in grp.get("work-summary", []):
            work: dict[str, Any] = {
                "title": _get(summary, "title", "title", "value", default=""),
                "year": None,
                "pmid": None,
                "doi": None,
                "type": summary.get("type"),
            }
            # Publication year
            year_value = _get(summary, "publication-date", "year", "value")
            if year_value is not None:
                try:
                    work["year"] = int(year_value)
                except (TypeError, ValueError):
                    work["year"] = None

            # External IDs
            ext_ids = _get(summary, "external-ids", "external-id", default=[])
            for eid in ext_ids:
                id_type = _get(eid, "external-id-type", default="").lower()
                id_value = eid.get("external-id-value", "")
                if id_type == "pmid":
                    work["pmid"] = id_value
                elif id_type == "doi":
                    work["doi"] = id_value

            works.append(work)
    return works
