"""PubMed and PMC fetching service with rate limiting."""

import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV_BASE = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles"

# Rate limiting: with API key 10/s, without 3/s
_request_semaphore = asyncio.Semaphore(8)  # Conservative limit


async def _ncbi_get(url: str, params: dict[str, Any]) -> httpx.Response:
    """Make a rate-limited GET request to NCBI."""
    settings = get_settings()
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    async with _request_semaphore:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            await asyncio.sleep(0.12)  # ~8 req/s
            return resp


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

        # PMCID from ArticleIdList
        for art_id in article.findall(".//ArticleId"):
            if art_id.get("IdType") == "pmc":
                record["pmcid"] = art_id.text
            elif art_id.get("IdType") == "doi":
                record["doi"] = art_id.text

        # Title
        title_el = article.find(".//ArticleTitle")
        record["title"] = (title_el.text or "") if title_el is not None else ""

        # Abstract
        abstract_parts = []
        for abstract_el in article.findall(".//AbstractText"):
            label = abstract_el.get("Label")
            text = abstract_el.text or ""
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

        # Authors to determine position
        authors = article.findall(".//Author")
        record["author_count"] = len(authors)

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
