"""OpenAlex works-by-ORCID client (corpus stage S2).

OpenAlex is often the largest discovery source for sparse-ORCID PIs (the
2026-08-13 rehearsal: gill had 0 ORCID works vs 33 via OpenAlex) — exactly the
new-PI onboarding case. Its identity is NOT trusted here: the rehearsal's one
OpenAlex error linked a stranger's paper to a PI's ORCID, so every S2
candidate goes through the resolver's author-match gate. This module only
fetches and extracts identifiers, and RAISES on failure (a silent empty result
is a thin corpus, not an answer — audit M5).
"""

import logging

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_MAX_PAGES = 5  # 1,000 works — far past any realistic single-PI corpus


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam so tests can inject httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


def _pmid_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _doi_from_url(url: str | None) -> str | None:
    if not url:
        return None
    doi = url.split("doi.org/", 1)[-1].strip()
    return doi or None


async def fetch_works_by_orcid(orcid: str) -> list[dict]:
    """All OpenAlex works for an ORCID: ``{"pmid", "doi", "year"}`` each.

    Cursor-paginated; includes a ``mailto`` (OpenAlex's polite pool) when a
    contact email is configured.
    """
    settings = get_settings()
    out: list[dict] = []
    cursor = "*"
    async with _make_client() as client:
        for _ in range(_MAX_PAGES):
            params = {
                "filter": f"author.orcid:https://orcid.org/{orcid}",
                "per-page": str(_PER_PAGE),
                "cursor": cursor,
                "select": "ids,publication_year",
            }
            contact = getattr(settings, "ncbi_contact_email", None)
            if contact:
                params["mailto"] = contact
            resp = await client.get(OPENALEX_WORKS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            for work in data.get("results", []):
                ids = work.get("ids") or {}
                out.append(
                    {
                        "pmid": _pmid_from_url(ids.get("pmid")),
                        "doi": _doi_from_url(ids.get("doi")),
                        "year": work.get("publication_year"),
                    }
                )
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
    return out
