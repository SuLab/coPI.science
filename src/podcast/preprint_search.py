"""Preprint server search for the podcast pipeline.

Supports bioRxiv, medRxiv (via biorxiv.org content API) and arXiv.

Records returned use the same schema as PubMed records but with:
  - pmid:    prefixed ID  e.g. "biorxiv:2024.04.01.123456", "arxiv:2401.12345"
  - url:     canonical preprint URL
  - journal: "<Server> (preprint)"
  - source:  "biorxiv" | "medrxiv" | "arxiv"
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BIORXIV_API = "https://api.biorxiv.org/details"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = "http://www.w3.org/2005/Atom"

# arXiv categories relevant to biomedical / computational biology research
ARXIV_CATEGORIES = "cat:q-bio.BM OR cat:q-bio.GN OR cat:q-bio.MN OR cat:q-bio.QM OR cat:cs.LG"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_search_terms(queries: list[str]) -> list[str]:
    """Extract individual quoted terms from PubMed query strings."""
    terms: list[str] = []
    for q in queries:
        for match in re.findall(r'"([^"]+)"', q):
            if match not in terms:
                terms.append(match)
    # Fall back to bare words if no quoted terms
    if not terms:
        for q in queries:
            for word in q.split():
                w = word.strip('"\'')
                if len(w) > 4 and w.upper() not in ("AND", "OR", "NOT") and w not in terms:
                    terms.append(w)
    return terms[:12]


def _score_record(title: str, abstract: str, terms: list[str]) -> int:
    """Count how many search terms appear in title+abstract (case-insensitive)."""
    text = (title + " " + abstract).lower()
    return sum(1 for t in terms if t.lower() in text)


def _date_range(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv
# ---------------------------------------------------------------------------

async def _fetch_biorxiv_server(
    server: str,
    queries: list[str],
    days: int,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fetch recent preprints from bioRxiv or medRxiv and score against queries."""
    terms = _extract_search_terms(queries)
    if not terms:
        return []

    start_date, end_date = _date_range(days)
    url = f"{BIORXIV_API}/{server}/{start_date}/{end_date}/0/json"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("%s API request failed: %s", server, exc)
        return []

    collection = data.get("collection") or []
    if not isinstance(collection, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in collection:
        title = item.get("title") or ""
        abstract = item.get("abstract") or ""
        if not abstract:
            continue

        # The bioRxiv API date-range filter includes revised preprints; filter by
        # the item's own date so we only include recently posted/first-version papers.
        date_str = item.get("date") or ""
        if date_str:
            try:
                item_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if item_date < cutoff:
                    continue
            except ValueError:
                pass

        score = _score_record(title, abstract, terms)
        if score == 0:
            continue

        doi = item.get("doi") or ""
        doi_suffix = doi.removeprefix("10.1101/")
        record_id = f"{server}:{doi_suffix}"

        # Authors stored as semicolon-separated string
        authors_raw = item.get("authors") or ""
        authors_list = [a.strip() for a in authors_raw.split(";") if a.strip()]

        year_str = date_str[:4]
        year = int(year_str) if year_str.isdigit() else datetime.now(timezone.utc).year

        scored.append((score, {
            "pmid": record_id,
            "url": f"https://www.{server}.org/content/{doi}v1",
            "title": title,
            "abstract": abstract,
            "journal": f"{server.capitalize()} (preprint)",
            "year": year,
            "authors": authors_list,
            "pub_types": ["Preprint"],
            "source": server,
        }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:max_results]]


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

async def _fetch_arxiv(
    queries: list[str],
    days: int,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fetch recent preprints from arXiv matching researcher queries."""
    terms = _extract_search_terms(queries)
    if not terms:
        return []

    # Build arXiv search: keyword terms in abstract + category filter
    term_clause = " OR ".join(f'abs:"{t}"' for t in terms[:6])
    search_query = f"({term_clause}) AND ({ARXIV_CATEGORIES})"

    start_date, _ = _date_range(days)
    # arXiv date filter via submittedDate
    arxiv_date = start_date.replace("-", "") + "000000"

    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results * 2),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(ARXIV_API, params=params)
            resp.raise_for_status()
            xml_text = resp.text
    except Exception as exc:
        logger.warning("arXiv API request failed: %s", exc)
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("arXiv XML parse error: %s", exc)
        return []

    records: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for entry in root.findall(f"{{{ARXIV_NS}}}entry"):
        title_el = entry.find(f"{{{ARXIV_NS}}}title")
        summary_el = entry.find(f"{{{ARXIV_NS}}}summary")
        id_el = entry.find(f"{{{ARXIV_NS}}}id")
        published_el = entry.find(f"{{{ARXIV_NS}}}published")

        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
        abstract = (summary_el.text or "").strip() if summary_el is not None else ""
        arxiv_url = (id_el.text or "").strip() if id_el is not None else ""
        published_str = (published_el.text or "").strip() if published_el is not None else ""

        if not abstract or not arxiv_url:
            continue

        # Parse submission date and apply cutoff
        try:
            pub_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            if pub_dt < cutoff:
                continue
            year = pub_dt.year
        except ValueError:
            year = datetime.now(timezone.utc).year

        # Extract arxiv ID from URL like http://arxiv.org/abs/2401.12345v1
        arxiv_id = arxiv_url.split("/abs/")[-1].split("v")[0]

        authors_list = [
            (n_el.text or "").strip()
            for author in entry.findall(f"{{{ARXIV_NS}}}author")
            for n_el in [author.find(f"{{{ARXIV_NS}}}name")]
            if n_el is not None and n_el.text
        ]

        records.append({
            "pmid": f"arxiv:{arxiv_id}",
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "title": title,
            "abstract": abstract,
            "journal": "arXiv (preprint)",
            "year": year,
            "authors": authors_list,
            "pub_types": ["Preprint"],
            "source": "arxiv",
        })

        if len(records) >= max_results:
            break

    return records


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def fetch_preprint_candidates(
    queries: list[str],
    already_delivered: set[str],
    days: int = 14,
    max_total: int = 20,
) -> list[dict[str, Any]]:
    """Fetch preprints from bioRxiv, medRxiv, and arXiv.

    Returns records filtered against already_delivered, up to max_total total.
    Each record has the same schema as PubMed records with an added 'url' field.
    """
    import asyncio

    per_source = max(max_total // 3, 5)

    biorxiv_task = _fetch_biorxiv_server("biorxiv", queries, days, per_source)
    medrxiv_task = _fetch_biorxiv_server("medrxiv", queries, days, per_source)
    arxiv_task = _fetch_arxiv(queries, days, per_source)

    results = await asyncio.gather(biorxiv_task, medrxiv_task, arxiv_task, return_exceptions=True)

    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    source_names = ("bioRxiv", "medRxiv", "arXiv")
    for name, result in zip(source_names, results):
        if isinstance(result, Exception):
            logger.warning("Preprint fetch failed for %s: %s", name, result)
            continue
        for rec in result:
            pid = rec["pmid"]
            if pid not in already_delivered and pid not in seen_ids:
                seen_ids.add(pid)
                candidates.append(rec)

    logger.info(
        "Preprint candidates: %d total (%s)",
        len(candidates),
        ", ".join(
            f"{name}: {len(r) if not isinstance(r, Exception) else 'err'}"
            for name, r in zip(source_names, results)
        ),
    )
    return candidates[:max_total]
