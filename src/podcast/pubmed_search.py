"""PubMed query builder and search for the podcast pipeline."""

import asyncio
import logging
from typing import Any

import httpx

from src.config import get_settings
from src.services.pubmed import _ncbi_get, fetch_pubmed_records

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def build_queries(profile: dict[str, Any]) -> list[str]:
    """Build 2–3 PubMed search query strings from a researcher's profile fields.

    profile keys used: disease_areas, techniques, experimental_models, keywords
    """
    disease_areas: list[str] = profile.get("disease_areas") or []
    techniques: list[str] = profile.get("techniques") or []
    experimental_models: list[str] = profile.get("experimental_models") or []
    keywords: list[str] = profile.get("keywords") or []

    queries: list[str] = []

    # Query 1: disease areas (most specific to the field)
    da_terms = [_simplify_term(t) for t in disease_areas[:6] if t]
    da_terms = [t for t in da_terms if t and len(t.split()) <= 5]
    if da_terms:
        queries.append(" OR ".join(f'"{t}"' for t in da_terms[:4]))

    # Query 2: techniques + experimental models (finds methods papers)
    tech_terms = [_simplify_term(t) for t in techniques[:4] if t]
    tech_terms = [t for t in tech_terms if t and len(t.split()) <= 4]
    if tech_terms:
        queries.append(" OR ".join(f'"{t}"' for t in tech_terms[:4]))

    # Query 3: keywords (broad coverage)
    kw_terms = [_simplify_term(t) for t in keywords[:8] if t]
    kw_terms = [t for t in kw_terms if t and len(t.split()) <= 4]
    if kw_terms:
        queries.append(" OR ".join(f'"{t}"' for t in kw_terms[:5]))

    # Fallback: use research summary words if nothing else
    if not queries:
        summary = profile.get("research_summary") or ""
        words = [w.strip(".,;:") for w in summary.split() if len(w) > 6][:5]
        if words:
            queries.append(" OR ".join(f'"{w}"' for w in words))

    return queries


def _simplify_term(term: str) -> str:
    """Strip parenthetical qualifiers and trim whitespace."""
    return term.split("(")[0].strip()


async def search_recent_pmids(
    queries: list[str],
    days: int = 14,
    max_total: int = 50,
) -> list[str]:
    """Run PubMed ESearch for each query, return deduplicated list of recent PMIDs."""
    settings = get_settings()
    seen: set[str] = set()
    pmids: list[str] = []

    # Date filter: last N days
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    date_filter = f"{cutoff}:{today}[pdat]"

    for query in queries:
        if len(pmids) >= max_total:
            break
        try:
            params = {
                "db": "pubmed",
                "term": f"({query}) AND {date_filter}",
                "retmode": "json",
                "retmax": str(max_total),
                "sort": "relevance",
            }
            resp = await _ncbi_get(f"{EUTILS_BASE}/esearch.fcgi", params)
            data = resp.json()
            ids = data.get("esearchresult", {}).get("idlist", [])
            for pid in ids:
                if pid not in seen and len(pmids) < max_total:
                    seen.add(pid)
                    pmids.append(pid)
            logger.debug("Query '%s': %d results", query[:60], len(ids))
        except Exception as exc:
            logger.warning("PubMed search failed for query '%s': %s", query[:60], exc)

    logger.info("Found %d candidate PMIDs across %d queries", len(pmids), len(queries))
    return pmids


async def fetch_candidates(
    queries: list[str],
    already_delivered: set[str],
    days: int = 14,
    max_total: int = 50,
) -> list[dict[str, Any]]:
    """Search PubMed and preprint servers, return candidate records excluding already-delivered IDs.

    Returns list of dicts with: pmid, title, abstract, journal, year, pub_types.
    Preprint records also include a 'url' and 'source' field.
    """
    from src.podcast.preprint_search import fetch_preprint_candidates

    # Fetch PubMed and preprints concurrently
    pubmed_pmids_task = search_recent_pmids(queries, days=days, max_total=max_total * 2)
    preprint_task = fetch_preprint_candidates(
        queries,
        already_delivered=already_delivered,
        days=days,
        max_total=max(max_total // 3, 10),
    )

    pmids_raw, preprint_candidates = await asyncio.gather(pubmed_pmids_task, preprint_task)

    # Filter PubMed results
    pmids = [p for p in pmids_raw if p not in already_delivered]
    pubmed_records = await fetch_pubmed_records(pmids[:max_total]) if pmids else []

    # Filter out reviews/editorials and items without abstracts from PubMed
    pubmed_candidates = []
    for rec in pubmed_records:
        if not rec.get("abstract"):
            continue
        pub_types = [pt.lower() for pt in (rec.get("pub_types") or [])]
        if any(t in pt for t in ("review", "editorial", "comment", "letter") for pt in pub_types):
            continue
        pubmed_candidates.append(rec)

    candidates = pubmed_candidates + preprint_candidates
    logger.info(
        "%d total candidates (PubMed: %d, preprints: %d)",
        len(candidates),
        len(pubmed_candidates),
        len(preprint_candidates),
    )
    return candidates
