"""PatentsView (USPTO) prior-art search. US filings only — see the caveat in
src/agent/tools.py where results are surfaced. Mirrors src/services/grants.py.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://search.patentsview.org/api/v1/patent/"


def _api_key() -> str:
    return get_settings().patentsview_api_key


async def search_prior_art(query: str, limit: int = 10) -> list[dict[str, Any]] | None:
    """Search US patents by text. Never raises.

    Return contract distinguishes "could not search" from "searched, found nothing",
    because a scouting hub must NOT read an unconfigured/unreachable tool as evidence
    of novelty (see the caveat/handling in src/agent/tools.py):

    - ``None``  — the search could NOT be performed: no API key, or the endpoint was
      unreachable / errored / rate-limited / returned unparseable JSON. The endpoint
      host may also be gone: PatentsView migrated to the USPTO Open Data Portal
      (data.uspto.gov) on 2026-03-20, and ``search.patentsview.org`` no longer
      resolves. A DNS/connection failure surfaces here as ``None``.
    - ``[]``    — the search ran (HTTP 200, parsed) and matched zero US filings.
    - non-empty — the matched filings.
    """
    key = _api_key()
    if not key:
        logger.info("[patents] no PatentsView API key configured — cannot search")
        return None
    params = {
        "q": '{"_text_any":{"patent_title":"%s"}}' % query.replace('"', ""),
        "f": '["patent_id","patent_title","patent_date","patent_abstract","assignees.assignee_organization"]',
        "o": '{"size":%d}' % limit,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(SEARCH_URL, params=params, headers={"X-Api-Key": key})
            if resp.status_code == 429:
                logger.warning("[patents] rate limited (429) — treating as unavailable")
                return None
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("[patents] search unavailable (endpoint unreachable or error): %s", exc)
        return None
    hits = []
    for p in data.get("patents", []) or []:
        hits.append({
            "patent_id": p.get("patent_id", ""),
            "title": p.get("patent_title", ""),
            "date": p.get("patent_date", ""),
            "abstract": p.get("patent_abstract", ""),
            "assignees": [a.get("assignee_organization", "")
                          for a in (p.get("assignees") or []) if a.get("assignee_organization")],
        })
    return hits
