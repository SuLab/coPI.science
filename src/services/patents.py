"""US prior-art search via the USPTO Open Data Portal (api.uspto.gov).

US filings only — see the caveat in src/agent/tools.py where results are surfaced.

Endpoint history: this tool originally targeted PatentsView
(search.patentsview.org), which PatentsView decommissioned in its 2026-03-20
migration to the USPTO Open Data Portal. The current endpoint is the ODP Patent
File Wrapper (PFW) search at api.uspto.gov. The response is application-centric
(patentFileWrapperDataBag) rather than granted-patent-centric, so we map the
applicationMetaData fields that matter for prior-art scouting (title, publication
number, dates, applicant, inventor, status). Abstracts are not returned by the
search endpoint.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"

# Simplified-query-syntax special characters; strip them from user text so the
# query cannot be broken or injected. Titles are matched on alnum + spaces.
_Q_SANITISE = re.compile(r"[^A-Za-z0-9 ]+")

# Full-text (abstract + first claim) enrichment. Each pre-grant-publication XML is
# ~1 MB, so we only fetch it for the top few hits and cap the extracted text to keep
# the Phase-4 prompt lean. Best-effort: a failed fetch leaves the hit title-level.
_FULLTEXT_MAX = 5
_ABSTRACT_LEN = 1200
_CLAIM_LEN = 600
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ABSTRACT_RE = re.compile(r"<abstract\b[^>]*>(.*?)</abstract>", re.DOTALL | re.IGNORECASE)
_CLAIM_RE = re.compile(r"<claim\b[^>]*>(.*?)</claim>", re.DOTALL | re.IGNORECASE)


def _api_key() -> str:
    s = get_settings()
    return s.uspto_api_key or s.patentsview_api_key


def _plaintext(fragment: str, limit: int) -> str:
    # Strip XML tags, then decode entities (the pgpub XML uses &#x3e; etc.).
    return html.unescape(_WS.sub(" ", _TAG.sub(" ", fragment)).strip())[:limit]


async def _fetch_fulltext(client: httpx.AsyncClient, uri: str) -> tuple[str, str]:
    """Fetch a pre-grant-publication XML and extract (abstract, first claim).

    Best-effort: returns ("", "") on any failure. The fileLocationURI 302-redirects
    to a signed data.uspto.gov download, so the client must follow redirects.
    """
    try:
        r = await client.get(uri, headers={"X-API-KEY": _api_key()})
        r.raise_for_status()
        xml = r.text
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("[patents] full-text fetch failed for %s: %s", uri, exc)
        return "", ""
    abstract = ""
    m = _ABSTRACT_RE.search(xml)
    if m:
        abstract = _plaintext(m.group(1), _ABSTRACT_LEN)
    claim = ""
    cm = _CLAIM_RE.search(xml)
    if cm:
        claim = _plaintext(cm.group(1), _CLAIM_LEN)
    return abstract, claim


async def search_prior_art(query: str, limit: int = 10) -> list[dict[str, Any]] | None:
    """Search US patent filings (USPTO ODP) by invention title. Never raises.

    Return contract distinguishes "could not search" from "searched, found nothing",
    because a scouting hub must NOT read an unconfigured/unreachable tool as evidence
    of novelty (see the caveat/handling in src/agent/tools.py):

    - ``None``  — the search could NOT be performed: no API key, or the endpoint was
      unreachable / errored / rate-limited / returned unparseable JSON.
    - ``[]``    — the search ran (HTTP 200, parsed) and matched zero US filings.
    - non-empty — the matched filings, newest first, each a dict with keys
      ``patent_id`` (publication/application number), ``title``, ``date``,
      ``applicant``, ``inventor``, ``status``, plus ``abstract`` and ``claim``
      (first claim) for the top ``_FULLTEXT_MAX`` published hits — "" when the
      pre-grant-publication full text is unavailable.
    """
    key = _api_key()
    if not key:
        logger.info("[patents] no USPTO API key configured — cannot search")
        return None
    tokens = _Q_SANITISE.sub(" ", query or "").split()
    if not tokens:
        return []
    # AND the title tokens. A space-separated list is OR'd by the ODP simplified
    # syntax, which for a multi-word concept ("CRISPR base editing") returns tens of
    # thousands of junk matches on common tokens ("base"). AND keeps precision:
    # measured 61,755 (OR) vs 17 (AND) on that query, the 17 all on-topic.
    q = "applicationMetaData.inventionTitle:(%s)" % " AND ".join(tokens)
    body = {
        "q": q,
        "pagination": {"offset": 0, "limit": max(1, min(limit, 50))},
        "sort": [{"field": "applicationMetaData.filingDate", "order": "desc"}],
    }
    # One client, redirect-following (the full-text XML 302s to a signed URL).
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.post(SEARCH_URL, json=body, headers={"X-API-KEY": key})
            if resp.status_code == 429:
                logger.warning("[patents] rate limited (429) — treating as unavailable")
                return None
            # ODP answers 404 (not 200-with-empty) when a valid search matches
            # nothing. That is "searched, found nothing" ([]), NOT "could not
            # search" (None) — critical so real novelty reads as [] not UNAVAILABLE.
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()

            hits: list[dict[str, Any]] = []
            fulltext_uris: list[str | None] = []
            for entry in data.get("patentFileWrapperDataBag", []) or []:
                meta = entry.get("applicationMetaData", {}) or {}
                number = (
                    meta.get("earliestPublicationNumber")
                    or entry.get("applicationNumberText")
                    or ""
                )
                hits.append({
                    "patent_id": number,
                    "title": meta.get("inventionTitle", ""),
                    "date": meta.get("earliestPublicationDate") or meta.get("filingDate", ""),
                    "applicant": meta.get("firstApplicantName", ""),
                    "inventor": meta.get("firstInventorName", ""),
                    "status": meta.get("applicationStatusDescriptionText", ""),
                    "abstract": "",
                    "claim": "",
                })
                fulltext_uris.append(
                    (entry.get("pgpubDocumentMetaData") or {}).get("fileLocationURI")
                )

            # Enrich the top few published hits with abstract + first claim. Bounded
            # and best-effort so a slow/missing XML never fails the search.
            for i in range(min(len(hits), _FULLTEXT_MAX)):
                if fulltext_uris[i]:
                    hits[i]["abstract"], hits[i]["claim"] = await _fetch_fulltext(
                        client, fulltext_uris[i]
                    )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("[patents] search unavailable (endpoint unreachable or error): %s", exc)
        return None

    return hits
