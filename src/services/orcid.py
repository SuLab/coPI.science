"""ORCID API client — fetch profile, grants, and works."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ORCID_API_BASE = "https://pub.orcid.org/v3.0"


async def fetch_orcid_record(orcid_id: str) -> dict[str, Any]:
    """Fetch full ORCID record for a given ORCID ID."""
    url = f"{ORCID_API_BASE}/{orcid_id}/record"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def fetch_orcid_profile(orcid_id: str) -> dict[str, Any]:
    """Extract name, affiliation, and email from ORCID record."""
    record = await fetch_orcid_record(orcid_id)
    result: dict[str, Any] = {"orcid": orcid_id}

    # Name
    name_block = record.get("person", {}).get("name", {})
    given = name_block.get("given-names", {}).get("value", "") if name_block else ""
    family = name_block.get("family-name", {}).get("value", "") if name_block else ""
    result["name"] = f"{given} {family}".strip() or orcid_id

    # Email (first public email)
    emails = (
        record.get("person", {})
        .get("emails", {})
        .get("email", [])
    )
    for e in emails:
        if e.get("primary") or not result.get("email"):
            result["email"] = e.get("email")

    # Current employment (affiliation) — prefer primary (lowest display-index)
    employments = (
        record.get("activities-summary", {})
        .get("employments", {})
        .get("affiliation-group", [])
    )
    # Full employment list (org, start year, current) — the tenure derivation
    # in src/services/jhu_rules.py needs every stint, ended ones included.
    all_employments: list[dict[str, Any]] = []
    current_employments: list[dict[str, Any]] = []
    for grp in employments:
        summaries_list = grp.get("summaries", [])
        if summaries_list:
            emp = summaries_list[0].get("employment-summary", {})
            start_year = None
            year_val = ((emp.get("start-date") or {}).get("year") or {}).get("value")
            if year_val:
                try:
                    start_year = int(year_val)
                except (TypeError, ValueError):
                    start_year = None
            all_employments.append(
                {
                    "organization": (emp.get("organization") or {}).get("name"),
                    "start_year": start_year,
                    "current": emp.get("end-date") is None,
                }
            )
        for summaries in summaries_list:
            emp = summaries.get("employment-summary", {})
            if emp.get("end-date") is None:  # Current employment
                current_employments.append(emp)
                break  # one per group
    # Sort by display-index ascending: 0 = primary/preferred position
    current_employments.sort(key=lambda e: int(e.get("display-index", 999)))
    if current_employments:
        emp = current_employments[0]
        org = emp.get("organization", {})
        result["institution"] = org.get("name")
        dept = emp.get("department-name")
        if dept:
            result["department"] = dept
    result["employments"] = all_employments

    # Researcher URLs (lab website)
    urls = record.get("person", {}).get("researcher-urls", {}).get("researcher-url", [])
    for u in urls:
        result["lab_website"] = u.get("url", {}).get("value")
        break

    return result


async def fetch_orcid_grants(orcid_id: str) -> list[str]:
    """Return list of grant titles from ORCID fundings."""
    url = f"{ORCID_API_BASE}/{orcid_id}/fundings"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch ORCID grants for %s: %s", orcid_id, exc)
            return []

    titles = []
    for grp in data.get("group", []):
        for summary in grp.get("funding-summary", []):
            title = summary.get("title", {}).get("title", {}).get("value")
            if title:
                titles.append(title)
    return titles


async def fetch_orcid_works(orcid_id: str) -> list[dict[str, Any]]:
    """Return list of works (publications) from ORCID, with PMIDs/DOIs."""
    url = f"{ORCID_API_BASE}/{orcid_id}/works"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch ORCID works for %s: %s", orcid_id, exc)
            return []

    works = []
    for grp in data.get("group", []):
        for summary in grp.get("work-summary", []):
            work: dict[str, Any] = {
                "title": summary.get("title", {}).get("title", {}).get("value", ""),
                "year": None,
                "pmid": None,
                "doi": None,
                "type": summary.get("type"),
            }
            # Publication year
            pub_date = summary.get("publication-date", {})
            if pub_date and pub_date.get("year"):
                work["year"] = int(pub_date["year"]["value"])

            # External IDs
            ext_ids = summary.get("external-ids", {}).get("external-id", [])
            for eid in ext_ids:
                id_type = eid.get("external-id-type", "").lower()
                id_value = eid.get("external-id-value", "")
                if id_type == "pmid":
                    work["pmid"] = id_value
                elif id_type == "doi":
                    work["doi"] = id_value

            works.append(work)
    return works
