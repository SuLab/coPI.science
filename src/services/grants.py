"""Grants.gov API client — search for open federal funding opportunities."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"

# Agencies most relevant to biomedical research
BIOMEDICAL_AGENCIES = ["HHS", "NSF"]


async def search_opportunities(
    keyword: str,
    agencies: list[str] | None = None,
    rows: int = 25,
    start: int = 0,
) -> list[dict[str, Any]]:
    """Search Grants.gov for open (posted) funding opportunities.

    Returns a list of opportunity dicts with keys:
        id, number, title, agency, open_date, close_date, description
    """
    payload = {
        "keyword": keyword,
        "oppStatuses": "posted",
        "rows": rows,
        "startRecordNum": start,
    }
    if agencies:
        payload["agencies"] = "|".join(agencies)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(SEARCH_URL, json=payload)
        resp.raise_for_status()
        raw = resp.json()

    # Response is nested: {errorcode, msg, data: {hitCount, oppHits: [...]}}
    data = raw.get("data", raw)
    hits = data.get("oppHits", [])
    results = []
    for hit in hits:
        results.append({
            "id": hit.get("id"),
            "number": hit.get("number", ""),
            "title": hit.get("title", ""),
            "agency": hit.get("agencyCode", ""),
            "open_date": hit.get("openDate", ""),
            "close_date": hit.get("closeDate", ""),
            "description": hit.get("description", ""),
        })

    logger.info(
        "Grants.gov search '%s': %d hits (showing %d)",
        keyword, data.get("hitCount", 0), len(results),
    )
    return results


async def fetch_opportunity_detail(opp_id: str) -> dict[str, Any] | None:
    """Fetch full details for a single opportunity by its Grants.gov ID."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(DETAIL_URL, json={"oppId": opp_id})
        resp.raise_for_status()
        raw = resp.json()

    data = raw.get("data", raw)
    # The detail endpoint sometimes returns an error message instead of data
    opp = data if isinstance(data, dict) and data.get("number") else None
    if not opp:
        return None

    return {
        "id": opp.get("id"),
        "number": opp.get("number", ""),
        "title": opp.get("title", ""),
        "agency": opp.get("agencyCode", ""),
        "description": opp.get("description", ""),
        "open_date": opp.get("openDate", ""),
        "close_date": opp.get("closeDate", ""),
        "award_ceiling": opp.get("awardCeiling"),
        "award_floor": opp.get("awardFloor"),
        "category": opp.get("categoryOfFundingActivity", ""),
        "eligibility": opp.get("eligibleApplicants", ""),
        "additional_info_url": opp.get("additionalInformationUrl", ""),
        "synopsis": opp.get("synopsis", {}).get("synopsisDesc", "") if isinstance(opp.get("synopsis"), dict) else "",
    }


async def fetch_opportunity_by_number(opp_number: str) -> dict[str, Any] | None:
    """Look up a funding opportunity by its FOA number (e.g., RFA-AI-27-019).

    Searches by keyword and matches on number. Returns full detail if found.
    """
    results = await search_opportunities(keyword=opp_number, rows=5)
    for r in results:
        if r.get("number", "").upper() == opp_number.upper():
            if r.get("id"):
                detail = await fetch_opportunity_detail(str(r["id"]))
                if detail:
                    return detail
            return r
    return None


async def search_for_researchers(
    researcher_keywords: dict[str, list[str]],
    agencies: list[str] | None = None,
    max_per_query: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """Search grants for multiple researchers' keyword sets.

    Args:
        researcher_keywords: {agent_id: [keyword1, keyword2, ...]}
        agencies: agency filter (defaults to BIOMEDICAL_AGENCIES)
        max_per_query: max results per keyword query

    Returns:
        {agent_id: [opportunity, ...]} — deduplicated by opportunity number
    """
    if agencies is None:
        agencies = BIOMEDICAL_AGENCIES

    results: dict[str, list[dict]] = {}
    seen_globally: set[str] = set()

    for agent_id, keywords in researcher_keywords.items():
        agent_opps: list[dict] = []
        seen_for_agent: set[str] = set()

        for keyword in keywords:
            try:
                opps = await search_opportunities(
                    keyword=keyword,
                    agencies=agencies,
                    rows=max_per_query,
                )
                for opp in opps:
                    opp_num = opp.get("number", "")
                    if opp_num and opp_num not in seen_for_agent:
                        seen_for_agent.add(opp_num)
                        opp["matched_keyword"] = keyword
                        agent_opps.append(opp)
                        seen_globally.add(opp_num)
            except Exception as exc:
                logger.warning("Grant search failed for '%s': %s", keyword, exc)

        results[agent_id] = agent_opps

    logger.info(
        "Grant search complete: %d unique opportunities across %d researchers",
        len(seen_globally), len(researcher_keywords),
    )
    return results
