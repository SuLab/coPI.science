"""PubMed and PMC fetching service with rate limiting."""

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from src.config import get_settings
from src.services.http_retry import get_with_retry

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV_BASE = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles"

# Strips a leading "doi:" or a doi.org URL prefix from a raw DOI string.
_DOI_PREFIX_RE = re.compile(r"^\s*(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)


def normalize_doi(doi: str | None) -> str | None:
    """Canonicalize a raw DOI string.

    Strips a leading ``doi:`` or ``https://doi.org/`` prefix and trailing
    whitespace/period junk. Case is preserved (DOIs are case-insensitive but the
    publisher-registered form often carries case). Returns ``None`` for empty or
    missing input.
    """
    if not doi:
        return None
    d = _DOI_PREFIX_RE.sub("", doi.strip()).strip()
    d = d.rstrip(" .")
    return d or None


def reconcile_pub_doi(
    assigned_doi: str | None, authoritative_doi: str | None
) -> tuple[str | None, str]:
    """Validate an assigned DOI against the DOI registered for its PMID.

    ``authoritative_doi`` is the DOI PubMed has on record for the publication's
    PMID — the trustworthy answer for "which paper is this PMID". Returns
    ``(final_doi, action)`` where action is one of:

    - ``"ok"``:         assigned matches authoritative (returned in canonical form)
    - ``"filled"``:     no assigned DOI; authoritative used
    - ``"corrected"``:  assigned disagreed with authoritative; authoritative used
    - ``"unverified"``: no authoritative DOI to check against; assigned kept
    - ``"none"``:       neither present

    The gate never persists a DOI that disagrees with its PMID's authoritative
    record: on a verifiable mismatch it returns the authoritative DOI, and on a
    match it canonicalizes (so format drift like ``doi:`` prefixes or
    slash-vs-dot corruption is fixed too).
    """
    auth = normalize_doi(authoritative_doi)
    assigned = normalize_doi(assigned_doi)
    if auth is None and assigned is None:
        return None, "none"
    if auth is None:
        return assigned, "unverified"
    if assigned is None:
        return auth, "filled"
    if assigned.lower() == auth.lower():
        # Already the right DOI — keep the stored form (DOIs are
        # case-insensitive, and the stored form is often better-cased than
        # esummary's lowercased value). Prefix/junk is already stripped above.
        return assigned, "ok"
    return auth, "corrected"

# Rate limiting: NCBI's policy caps anonymous traffic at 3 req/s and API-keyed
# traffic at 10 req/s. Two long-lived semaphores (rather than one resized at
# call time) because the key/no-key split is a per-process constant. Constructing
# a Semaphore outside a running loop is safe in py3.10+, but that alone does not
# make these loop-independent — like `asyncio.Lock` (see the comment above
# `_ncbi_next_start`), a Semaphore binds to a loop at its first *contended*
# acquire, not at construction. The semaphore only bounds how many NCBI requests
# may be in flight at once — it does NOT bound the aggregate rate: N slots each
# sleeping `interval` seconds in the old code allowed up to N/interval requests
# per second, well past NCBI's ceiling (keyless peaked at ~5.9 req/s against a 3
# req/s limit). The actual ceiling is enforced by `_pace_ncbi` below, a
# module-level monotonic-clock gate that spaces request STARTS at least
# `interval` seconds apart, process-wide, regardless of how many callers are in
# flight.
# HAZARD (review round 2, deferred): bound at its first contended acquire, so the
# process must not call asyncio.run() twice with >= N concurrent NCBI calls at
# the same has_key value (N = that semaphore's size) — a per-loop semaphore map
# is the full fix; test_pubmed_contract.py mitigates this test-only by rebinding
# fresh semaphores every test.
_NCBI_SEMAPHORES = {True: asyncio.Semaphore(8), False: asyncio.Semaphore(2)}
_NCBI_PACING_SECONDS = {True: 0.12, False: 0.34}  # 8.3 req/s / 2.9 req/s aggregate ceilings

# Overridable by tests (see test_pubmed_contract.py) — the retry loop's own
# exponential backoff, not the pacing gate above.
_RETRY_BACKOFF = 0.5

# Shared clock state for `_pace_ncbi`: a monotonic-clock reservation cursor, not a
# lock. An `asyncio.Lock` does not bind to a loop at construction — lazy
# construction was never the fix — it binds at its first *contended* acquire,
# inside `await lock.acquire()`. A module-level singleton lock acquired from more
# than one event loop over the process's life (a fresh loop per `asyncio.run()`
# call, or per test with function-scoped event loops) eventually gets acquired
# from a second loop and raises. The cursor below needs no lock: every caller
# does one read-modify-write of `_ncbi_next_start` with no `await` between the
# read and the write, which is atomic on a single-threaded event loop — nothing
# else can run between two non-`await` statements — so concurrent callers can't
# race it, and the cursor itself is never bound to any loop (unlike the
# semaphores above, which share the Lock's first-contended-acquire hazard — see
# the HAZARD note above `_NCBI_SEMAPHORES`).
_ncbi_next_start = 0.0


async def _pace_ncbi(interval: float) -> None:
    """Space NCBI request starts at least `interval` seconds apart, process-wide.

    Enforces the E-utilities ceiling (3 req/s anonymous, 10 req/s with an api_key)
    regardless of how many callers are in flight — the semaphore alone does not:
    N slots each sleeping `interval` allow N/interval requests per second. Reserves
    the next start slot on `_ncbi_next_start` with a single atomic
    read-modify-write (no lock, nothing loop-bound — see the module-level comment
    above), then sleeps out whatever wait that reservation implies. (#23 COR-29b,
    review fix round 2.)
    """
    global _ncbi_next_start
    now = time.monotonic()
    start = max(now, _ncbi_next_start)
    _ncbi_next_start = start + interval
    if start > now:
        await asyncio.sleep(start - now)


# NCBI's E-utilities usage policy requires every request to identify the caller with
# `tool` and `email`. Anonymous traffic is throttled first and IP-blocked second, and
# NCBI has no way to warn us because it does not know who we are. Every NCBI call in
# the system — the profile pipeline, DOI reconciliation, PMC methods extraction —
# funnels through _ncbi_get, so omitting these made the whole deployment anonymous.
_NCBI_TOOL = "copi-science"


async def _ncbi_get(url: str, params: dict[str, Any]) -> httpx.Response:
    """Make a rate-limited, identified, retried GET request to NCBI.

    Retries a transient failure (COR-29a) through the shared ``get_with_retry`` helper. Pacing
    (COR-29b) happens via ``_pace_ncbi`` immediately before the request is sent, inside the
    semaphore slot, so it gates every call's start regardless of outcome. Retried attempts inside
    ``get_with_retry`` are not paced again here — its own exponential backoff (>=0.5s) already
    exceeds the pacing interval (<=0.34s), so it never presses harder on the ceiling than a single
    paced call would.
    """
    settings = get_settings()
    has_key = bool(settings.ncbi_api_key)
    if has_key:
        params["api_key"] = settings.ncbi_api_key
    params.setdefault("tool", _NCBI_TOOL)
    params.setdefault("email", settings.ncbi_contact_email or settings.ses_sender_email)
    async with _NCBI_SEMAPHORES[has_key]:
        await _pace_ncbi(_NCBI_PACING_SECONDS[has_key])
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            return await get_with_retry(client, url, params=params, backoff=_RETRY_BACKOFF)


async def fetch_pubmed_records(pmids: list[str]) -> list[dict[str, Any]]:
    """
    Batch fetch PubMed records for a list of PMIDs.
    Returns list of dicts with: pmid, title, abstract, journal, year, author_position.
    """
    if not pmids:
        return []

    # Batch in chunks of 100
    results = []
    for i in range(0, len(pmids), 100):
        batch = pmids[i : i + 100]
        try:
            records = await _fetch_pubmed_batch(batch)
            results.extend(records)
        except Exception as exc:
            logger.error("Failed to fetch PubMed batch %s: %s", batch[:3], exc)
    return results


async def fetch_authoritative_dois(pmids: list[str]) -> dict[str, str]:
    """Return ``{pmid: doi}`` — the DOI PubMed has on record for each PMID.

    Uses the esummary endpoint, whose ``articleids`` are strictly article-scoped
    (they never include the reference list), making this an authoritative source
    independent of the efetch XML parser. PMIDs with no record or no DOI on file
    are omitted. Used by the ingest gate's audit counterpart and
    ``scripts/audit_pub_dois.py``.
    """
    clean = [str(p) for p in pmids if p]
    if not clean:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(clean), 200):
        batch = clean[i : i + 200]
        try:
            resp = await _ncbi_get(
                f"{EUTILS_BASE}/esummary.fcgi",
                {"db": "pubmed", "id": ",".join(batch), "retmode": "json"},
            )
            result = resp.json().get("result", {})
        except Exception as exc:
            logger.warning("esummary batch failed (%s...): %s", batch[:3], exc)
            continue
        for pmid in result.get("uids", []):
            for aid in result.get(pmid, {}).get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = normalize_doi(aid.get("value"))
                    if doi:
                        out[str(pmid)] = doi
                    break
    return out


async def _fetch_pubmed_batch(pmids: list[str]) -> list[dict[str, Any]]:
    """Fetch a batch of PubMed records (max 100)."""
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "xml",
        "retmode": "xml",
    }
    resp = await _ncbi_get(f"{EUTILS_BASE}/efetch.fcgi", params)
    return _parse_pubmed_xml(resp.text)


def _parse_pubmed_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse PubMed XML efetch response."""
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("Failed to parse PubMed XML: %s", exc)
        return results

    for article in root.findall(".//PubmedArticle"):
        record: dict[str, Any] = {}

        # PMID
        pmid_el = article.find(".//PMID")
        if pmid_el is not None:
            record["pmid"] = pmid_el.text

        # DOI / PMCID: read ONLY the article's own <ArticleIdList> (under
        # <PubmedData>). A recursive ".//ArticleId" search also matches the
        # <ReferenceList>, whose ArticleIds belong to *cited* papers — taking
        # those (and the previous code kept the last match, deep in the
        # references) silently stamped the publication with a reference's DOI or
        # PMCID. This was the root cause of the bad paper links (issue #5):
        # the spurious DOIs were the most-cited references (CRISPResso2, DAVID,
        # …), not the article itself.
        id_container = article.find("./PubmedData/ArticleIdList")
        if id_container is not None:
            for art_id in id_container.findall("ArticleId"):
                id_type = art_id.get("IdType")
                if id_type == "pmc" and "pmcid" not in record:
                    record["pmcid"] = art_id.text
                elif id_type == "doi" and "doi" not in record:
                    record["doi"] = art_id.text
        # Article DOI can also appear as an ELocationID under <Article> (also
        # article-scoped, never a reference).
        if "doi" not in record:
            for eloc in article.findall("./MedlineCitation/Article/ELocationID"):
                if eloc.get("EIdType") == "doi" and eloc.text:
                    record["doi"] = eloc.text
                    break

        # Title. itertext() (not .text) so inline markup (<i>, <sub>, <sup> — gene
        # symbols, chemical formulas, italicized species names) doesn't truncate
        # the title at the first child element (issue #22 COR-16).
        title_el = article.find(".//ArticleTitle")
        record["title"] = "".join(title_el.itertext()) if title_el is not None else ""

        # Abstract — same markup-truncation defect as the title.
        abstract_parts = []
        for abstract_el in article.findall(".//AbstractText"):
            label = abstract_el.get("Label")
            text = "".join(abstract_el.itertext())
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        record["abstract"] = " ".join(abstract_parts)

        # Journal
        journal_el = article.find(".//Journal/Title")
        record["journal"] = journal_el.text if journal_el is not None else None

        # Year
        year_el = article.find(".//PubDate/Year")
        if year_el is not None and year_el.text:
            try:
                record["year"] = int(year_el.text)
            except ValueError:
                pass

        # Article type
        pub_types = [
            pt.text
            for pt in article.findall(".//PublicationType")
            if pt.text
        ]
        record["pub_types"] = pub_types

        # Authors — count for position heuristics, names for tool output.
        # Names let an agent CHECK authorship before claiming it (issue #29:
        # the origin exchange retrieved this very paper and the tool answer
        # had no author list to falsify the false co-authorship claim).
        authors = article.findall(".//Author")
        record["author_count"] = len(authors)
        names: list[str] = []
        for author in authors:
            collective = author.findtext("CollectiveName")
            if collective:
                names.append(collective.strip())
                continue
            last = author.findtext("LastName")
            if not last:
                continue
            initials = author.findtext("Initials")
            names.append(f"{last} {initials}".strip() if initials else last.strip())
        record["authors"] = names

        results.append(record)

    return results


async def convert_dois_to_pmids(dois: list[str]) -> dict[str, str]:
    """
    Convert DOIs to PMIDs. First tries NCBI ID converter (batch, but PMC-only),
    then falls back to PubMed ESearch for unresolved DOIs.
    Returns dict of {doi: pmid}.
    """
    if not dois:
        return {}

    mapping = {}

    # Phase 1: NCBI ID converter (batch — only finds PMC-indexed papers)
    for i in range(0, len(dois), 200):
        batch = dois[i : i + 200]
        try:
            params = {"ids": ",".join(batch), "format": "json"}
            resp = await _ncbi_get(IDCONV_BASE, params)
            data = resp.json()
            for record in data.get("records", []):
                if record.get("status") == "error":
                    continue
                doi = record.get("doi")
                pmid = record.get("pmid")
                if doi and pmid:
                    mapping[doi] = str(pmid)
        except Exception as exc:
            logger.warning("Failed batch DOI→PMID via ID converter: %s", exc)

    # Phase 2: PubMed ESearch for remaining DOIs
    remaining = [d for d in dois if d not in mapping]
    if remaining:
        logger.info("Resolving %d remaining DOIs via PubMed ESearch", len(remaining))
        for doi in remaining:
            try:
                params = {
                    "db": "pubmed",
                    "term": f"{doi}[doi]",
                    "retmode": "json",
                }
                resp = await _ncbi_get(f"{EUTILS_BASE}/esearch.fcgi", params)
                data = resp.json()
                id_list = data.get("esearchresult", {}).get("idlist", [])
                if id_list:
                    mapping[doi] = id_list[0]
            except Exception as exc:
                logger.debug("ESearch DOI lookup failed for %s: %s", doi, exc)

    return mapping


async def convert_pmids_to_pmcids(pmids: list[str]) -> dict[str, str]:
    """
    Convert PMIDs to PMCIDs using NCBI ID converter.
    Returns dict of {pmid: pmcid}.
    """
    if not pmids:
        return {}

    mapping = {}
    for i in range(0, len(pmids), 200):
        batch = pmids[i : i + 200]
        try:
            params = {"ids": ",".join(batch), "format": "json"}
            resp = await _ncbi_get(IDCONV_BASE, params)
            data = resp.json()
            for record in data.get("records", []):
                if record.get("status") == "error":
                    continue
                pmid = record.get("pmid")
                pmcid = record.get("pmcid")
                if pmid and pmcid:
                    mapping[str(pmid)] = pmcid
        except Exception as exc:
            logger.warning("Failed to convert PMIDs to PMCIDs: %s", exc)
    return mapping


async def fetch_pmc_methods(pmcid: str) -> str | None:
    """
    Fetch the methods section from a PMC full-text article.
    Returns extracted methods text or None if not available.
    """
    # Strip PMC prefix if present
    pmcid_clean = pmcid.replace("PMC", "")
    params = {
        "db": "pmc",
        "id": pmcid_clean,
        "rettype": "xml",
        "retmode": "xml",
    }
    try:
        resp = await _ncbi_get(f"{EUTILS_BASE}/efetch.fcgi", params)
        return _extract_methods_section(resp.text)
    except Exception as exc:
        logger.debug("Failed to fetch PMC full text for %s: %s", pmcid, exc)
        return None


def _extract_methods_section(xml_text: str) -> str | None:
    """Extract the methods/materials section text from PMC XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    methods_keywords = {
        "methods",
        "materials and methods",
        "experimental procedures",
        "experimental methods",
        "methods and materials",
        "star methods",
        "method details",
    }

    # Look for sections with methods-like titles
    for sec in root.findall(".//{http://jats.nlm.nih.gov}sec"):
        title_el = sec.find("{http://jats.nlm.nih.gov}title")
        if title_el is not None and title_el.text:
            if title_el.text.lower().strip() in methods_keywords:
                return _extract_text(sec)

    # Fallback: any <sec> with title containing "method"
    for sec in root.findall(".//{http://jats.nlm.nih.gov}sec"):
        title_el = sec.find("{http://jats.nlm.nih.gov}title")
        if title_el is not None and title_el.text:
            if "method" in title_el.text.lower():
                return _extract_text(sec)

    # Try without namespace
    for sec in root.findall(".//sec"):
        title_el = sec.find("title")
        if title_el is not None and title_el.text:
            if "method" in title_el.text.lower():
                return _extract_text(sec)

    return None


def _extract_text(element) -> str:
    """Recursively extract text from an XML element."""
    parts = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(_extract_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(p.strip() for p in parts if p.strip())


# ---------------------------------------------------------------------------
# High-level functions for agent tool use
# ---------------------------------------------------------------------------


async def fetch_abstract(pmid_or_doi: str) -> dict[str, Any]:
    """
    Fetch a paper's abstract given a PMID or DOI.

    Returns dict with: pmid, title, abstract, journal, year (or error key).
    """
    pmid = pmid_or_doi.strip()

    # If it looks like a DOI, resolve to PMID first
    if "/" in pmid or pmid.startswith("10."):
        mapping = await convert_dois_to_pmids([pmid])
        resolved = mapping.get(pmid)
        if not resolved:
            return {"error": f"Could not resolve DOI {pmid} to a PMID"}
        pmid = resolved

    records = await fetch_pubmed_records([pmid])
    if not records:
        return {"error": f"No PubMed record found for {pmid}"}

    rec = records[0]
    return {
        "pmid": rec.get("pmid", pmid),
        "title": rec.get("title", ""),
        "abstract": rec.get("abstract", ""),
        "journal": rec.get("journal", ""),
        "year": rec.get("year"),
        "authors": rec.get("authors", []),
        # The paper's own DOI (article-scoped, see _parse_pubmed_xml). Cited
        # by the retrieve tools so an agent sharing its own paper can satisfy
        # the emit gate's DOI requirement (issue #29).
        "doi": rec.get("doi", ""),
    }


async def fetch_full_text(pmid_or_doi: str) -> dict[str, Any]:
    """
    Fetch full text (methods section) for a paper given a PMID or DOI.

    Returns dict with: pmid, pmcid, title, abstract, methods (or error key).
    """
    # First get the abstract / metadata
    abstract_data = await fetch_abstract(pmid_or_doi)
    if "error" in abstract_data:
        return abstract_data

    pmid = abstract_data["pmid"]

    # Resolve PMID to PMCID
    pmcid_map = await convert_pmids_to_pmcids([pmid])
    pmcid = pmcid_map.get(pmid)
    if not pmcid:
        return {
            **abstract_data,
            "pmcid": None,
            "methods": None,
            "note": "Paper not available in PubMed Central (no free full text)",
        }

    # Fetch methods section
    methods = await fetch_pmc_methods(pmcid)
    return {
        **abstract_data,
        "pmcid": pmcid,
        "methods": methods,
    }
