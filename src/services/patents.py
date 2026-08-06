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
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"

# Simplified-query-syntax special characters; strip them from user text so the
# query cannot be broken or injected. Titles are matched on alnum + spaces.
_Q_SANITISE = re.compile(r"[^A-Za-z0-9 ]+")


@dataclass(frozen=True)
class PriorArtResult:
    """A completed title search. ``None`` from search_prior_art still means the
    search could not run at all — see that function's contract.

    ``terms_used`` is the breadth that produced ``hits``: when it is shorter than
    ``total_terms`` the query was broadened because the full phrase matched nothing,
    and the caller MUST say so rather than presenting the result as on-point.
    """

    hits: list[dict[str, Any]]
    terms_used: list[str]
    total_terms: int

    @property
    def broadened(self) -> bool:
        return len(self.terms_used) < self.total_terms


# Domain-generic words. A title search ANDing these in is what made every
# production query return zero: "inhibitor", "treatment" and "disease" are in
# almost no patent TITLE even when the patent is squarely on point.
_GENERIC = frozenset({
    "a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to", "via", "with",
    "using", "use", "uses", "based", "novel", "new", "improved", "method", "methods",
    "system", "systems", "approach", "approaches", "treatment", "treating", "therapy",
    "therapeutic", "therapeutics", "disease", "diseases", "disorder", "disorders",
    "patient", "patients", "human", "clinical", "cell", "cells", "protein", "proteins",
    "inhibitor", "inhibitors", "inhibiting", "inhibition", "modulator", "modulators",
    "agent", "agents", "targeting", "target", "targets", "assay", "assays", "platform",
    "expression", "activity", "function",
})

# Floor on how narrow a *multi*-term backoff goes before the final single-term
# tier (see _tiers). Two ANDed specific terms is still worth trying on its own
# before dropping to one, since a pairing of two real signal terms is more
# specific than either alone when both survive generics-filtering.
_MIN_TERMS = 2


def _salience(token: str) -> tuple[int, int, str]:
    """Rank key: gene/target symbols beat prose. Deterministic (ties break on the
    token itself) so the query sent to USPTO is reproducible across runs."""
    score = 0
    if any(ch.isdigit() for ch in token):
        score += 3  # C9orf72, MARK2, PE38, HER3
    if token.isupper() and len(token) >= 2:
        score += 3  # TFEB, BRAF, ALS
    elif not token.islower():
        score += 2  # MiP, mCherry
    score += min(len(token) // 4, 2)
    return (score, len(token), token)


def _rank_terms(tokens: list[str]) -> list[str]:
    specific = [t for t in tokens if t.lower() not in _GENERIC]
    pool = specific or tokens  # an all-generic query still gets searched
    return sorted(pool, key=_salience, reverse=True)


def _tiers(tokens: list[str]) -> list[list[str]]:
    """Breadths to try, widest first, at most four HTTP calls (the ODP
    rate-limits aggressively — a 429 costs us the whole search).

    Tier 1 is the query EXACTLY as asked, in the caller's own order: that is
    the precise search, and preserving it means the backoff only ever widens.
    Later tiers drop generic words and keep the most specific terms, ending at
    a single term: measured against the live USPTO API, a lone specific term
    (TFEB, BRAF, C9orf72) reliably returns hits where a 2-term floor returned
    zero for exactly the queries this backoff exists to fix. A long query can
    now cost up to four calls; a 429 on any of them still returns ``None``
    (see search_prior_art) rather than a clean-looking empty result.

    Widths are gated on ``candidate not in tiers`` alone, NOT on the input
    token count — gating on ``width >= len(tokens)`` (the pre-fix behaviour)
    skipped every backoff tier for a query of 2 tokens or fewer, which is
    exactly the breadth the prompt now asks the model to use.

    ``_MIN_TERMS`` is a floor on how narrow a *multi*-term backoff goes before
    the final single-term tier below it, not a minimum on how much specific
    signal a tier must contain: when the specific pool is narrower than a
    given width (e.g. only one gene symbol survives among several generic
    words), that narrower pool is still used, in full, as its own tier.
    Skipping it there would silently reproduce the guaranteed zero-hit bug
    this backoff exists to fix — a single specific term is more informative
    than the full generic-laden phrase it's paired with.
    """
    ranked = _rank_terms(tokens)
    tiers = [list(tokens)]
    for width in (3, _MIN_TERMS, 1):
        candidate = ranked[:width] if width <= len(ranked) else list(ranked)
        if candidate not in tiers:
            tiers.append(candidate)
    return tiers


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


async def _search_titles(
    client: httpx.AsyncClient, terms: list[str], limit: int, key: str
) -> list[dict[str, Any]] | None:
    """One title search. ``None`` == rate limited (caller must treat as unavailable);
    ``[]`` == searched, matched nothing. Each hit carries ``_pgpub_uri`` for the
    optional full-text enrichment, which the caller pops before returning.
    """
    body = {
        "q": "applicationMetaData.inventionTitle:(%s)" % " AND ".join(terms),
        "pagination": {"offset": 0, "limit": max(1, min(limit, 50))},
        "sort": [{"field": "applicationMetaData.filingDate", "order": "desc"}],
    }
    resp = await client.post(SEARCH_URL, json=body, headers={"X-API-KEY": key})
    if resp.status_code == 429:
        logger.warning("[patents] rate limited (429) — treating as unavailable")
        return None
    # ODP answers 404 (not 200-with-empty) when a valid search matches nothing.
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()

    hits: list[dict[str, Any]] = []
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
            "_pgpub_uri": (entry.get("pgpubDocumentMetaData") or {}).get("fileLocationURI"),
        })
    return hits


async def _enrich(client: httpx.AsyncClient, hits: list[dict[str, Any]]) -> None:
    """Add abstract + first claim to the top few published hits, in place. Bounded
    and best-effort so a slow or missing XML never fails the search.

    ``_pgpub_uri`` is internal bookkeeping (which pre-grant XML to fetch) and must
    never survive into a returned hit — it is popped unconditionally here, on every
    hit, even past ``_FULLTEXT_MAX`` where it is never fetched.
    """
    for i, hit in enumerate(hits):
        uri = hit.pop("_pgpub_uri", None)
        if uri and i < _FULLTEXT_MAX:
            hit["abstract"], hit["claim"] = await _fetch_fulltext(client, uri)


async def search_prior_art(query: str, limit: int = 10) -> PriorArtResult | None:
    """Search US patent filings (USPTO ODP) by invention title. Never raises.

    Tries the full phrase first, then progressively fewer, more specific terms
    (see ``_tiers``). This matters more than it looks: a free-text query ANDed on
    the title is a guaranteed zero-hit — measured 12/12 in production before the
    backoff existed — and a scouting hub reads a zero-hit as novelty.

    - ``None``             — the search could NOT be performed: no API key, or the
      endpoint was unreachable / errored / rate-limited / returned unparseable JSON.
    - ``PriorArtResult``   — the search ran. ``.hits`` may be empty (genuinely no
      title match at the narrowest breadth tried). ``.broadened`` tells the caller
      the query was widened and the hits may be adjacent rather than on point.
    """
    key = _api_key()
    if not key:
        logger.info("[patents] no USPTO API key configured — cannot search")
        return None
    tokens = _Q_SANITISE.sub(" ", query or "").split()
    if not tokens:
        return PriorArtResult(hits=[], terms_used=[], total_terms=0)

    tiers = _tiers(tokens)
    hits: list[dict[str, Any]] = []
    terms_used = tiers[-1]
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for terms in tiers:
                attempt = await _search_titles(client, terms, limit, key)
                if attempt is None:
                    return None
                if attempt:
                    hits, terms_used = attempt, terms
                    break
            if hits:
                await _enrich(client, hits)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("[patents] search unavailable (endpoint unreachable or error): %s", exc)
        return None

    if len(terms_used) < len(tokens):
        logger.info(
            "[patents] broadened %d terms -> %s (%d hits)",
            len(tokens), terms_used, len(hits),
        )
    return PriorArtResult(hits=hits, terms_used=list(terms_used), total_terms=len(tokens))
