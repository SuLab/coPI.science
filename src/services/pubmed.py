"""PubMed and PMC fetching service with rate limiting."""

import asyncio
import contextvars
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import get_settings
from src.services.http_pacing import Pacer

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# The trailing slash is load-bearing: without it NCBI answers 301 to the same path
# WITH one (live-verified 2026-08-22), so every lookup cost two requests. That was
# 388 requests for 194 lookups in run 8b64a0e0 — 32% of all our NCBI traffic — and
# because _pace() only counts the request it issues, the real rate against NCBI was
# twice what the pacer believed.
IDCONV_BASE = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"

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


def _doi_fold(doi: str) -> str:
    """Comparison key for two spellings of the same DOI.

    DOIs are case-insensitive (DOI Handbook §2.4) and may arrive with or
    without a ``doi:``/``https://doi.org/`` prefix, so equality checks fold
    both away. Never use this as a STORED form — ``normalize_doi`` preserves
    case on purpose (the publisher-registered form often carries it)."""
    return (normalize_doi(doi) or doi).casefold()


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

# Rate limiting: NCBI allows 10 req/s with an API key, 3 req/s without.
# Two separate bounds, deliberately: the semaphore caps CONCURRENCY (open
# sockets against NCBI), and _pace() caps the RATE by spacing request
# STARTS. The old design slept inside the semaphore, which bounds nothing:
# rate = concurrency / per-request-time, so 8 concurrent holders each
# pausing 0.12s could burst far past the keyless 3/s (issue #23 V9).
_request_semaphore = asyncio.Semaphore(3)


def _pace_interval() -> float:
    return 0.11 if get_settings().ncbi_api_key else 0.34


_PACER = Pacer(lambda: _pace_interval())  # the lambda keeps patching _pace_interval effective


async def _pace() -> None:
    """Space request starts at least _pace_interval() apart, process-wide (see http_pacing)."""
    await _PACER.wait()


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam so tests can inject httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


# NCBI's E-utilities usage policy requires every request to identify the caller with
# `tool` and `email`. Anonymous traffic is throttled first and IP-blocked second, and
# NCBI has no way to warn us because it does not know who we are. Every NCBI call in
# the system — the profile pipeline, DOI reconciliation, PMC methods extraction —
# funnels through _ncbi_get, so omitting these made the whole deployment anonymous.
_NCBI_TOOL = "copi-science"

# Transport-level failures NCBI actually produces under load. These carry no status
# code, so the status-code retry below cannot see them: a truncated response body
# surfaces as RemoteProtocolError and used to abort the call outright. In run
# 8b64a0e0 exactly one of those made fetch_abstract tell the model "No PubMed record
# found for 41130592" — a paper that exists and that the same run had fetched
# successfully 69 seconds earlier.
#
# Deliberately narrower than httpx.TransportError: every member here means "the
# request did not complete", which is retryable by definition, whereas
# UnsupportedProtocol / ProxyError / InvalidURL / LocalProtocolError mean the
# request was never well-formed and retrying it just triples the traffic behind
# a bug of ours.
#
# BASE classes, not leaf names. The tuple used to enumerate
# (RemoteProtocolError, ReadTimeout, ConnectError, ReadError), which left
# ConnectTimeout, WriteTimeout, PoolTimeout and WriteError uncaught while the
# sentence above claimed to cover them — four ways for a request that did not
# complete to be surfaced as a hard error and turned by `fetch_abstract` into
# "No PubMed record found for X". A base-class tuple is also the only form a
# test named "every timeout class is retried" can actually check, and it covers
# whatever httpx adds next on the day it appears
# (tests/unit/test_pubmed_transport.py).
#
# `RemoteProtocolError` stays named individually rather than widening to its
# `ProtocolError` base: that base's other child is `LocalProtocolError`, which
# is a malformed request of OURS.
_RETRYABLE_TRANSPORT = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


_SHARED_CLIENT: contextvars.ContextVar[httpx.AsyncClient | None] = contextvars.ContextVar(
    "pubmed_shared_client", default=None
)
# PMID -> record for every DOI verification `convert_dois_to_pmids` accepted inside an
# `ncbi_session()`, so the caller's later EFetch stage need not fetch those PMIDs again.
_VERIFIED_RECORDS: contextvars.ContextVar[dict[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "pubmed_verified_records", default=None
)


@asynccontextmanager
async def ncbi_session() -> AsyncIterator[httpx.AsyncClient]:
    """One httpx client for every NCBI request issued inside the block (DP-06):
    `resolve_corpus` wraps its retrieval in this, so a corpus reuses one connection
    pool instead of opening a client per request. Pacing and the semaphore are
    unchanged. The block also collects the records DOI verification fetched
    (`session_verified_records`)."""
    async with _make_client() as client:
        client_token = _SHARED_CLIENT.set(client)
        records_token = _VERIFIED_RECORDS.set({})
        try:
            yield client
        finally:
            _VERIFIED_RECORDS.reset(records_token)
            _SHARED_CLIENT.reset(client_token)


def session_verified_records() -> dict[str, dict[str, Any]]:
    """PMID -> record for the DOI verifications accepted so far inside the current
    `ncbi_session()` (a live dict; empty outside one)."""
    return _VERIFIED_RECORDS.get() or {}


async def _ncbi_get(url: str, params: dict[str, Any]) -> httpx.Response:
    """Make a rate-limited, identified GET request to NCBI, with retry.

    Pacing runs BEFORE raise_for_status — the old order skipped pacing
    exactly when NCBI was already 429-ing us. Retries cover the transient
    statuses NCBI actually emits under load *and* the transport failures in
    ``_RETRYABLE_TRANSPORT``; anything else raises as before.
    """
    settings = get_settings()
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    params.setdefault("tool", _NCBI_TOOL)
    params.setdefault("email", settings.ncbi_contact_email or settings.ses_sender_email)
    async with _request_semaphore:
        shared = _SHARED_CLIENT.get()
        if shared is not None:
            return await _ncbi_attempts(shared, url, params)
        async with _make_client() as client:
            return await _ncbi_attempts(client, url, params)


async def _ncbi_attempts(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> httpx.Response:
    """The paced, retried GET loop of `_ncbi_get`, on a given client."""
    for attempt in range(3):
        await _pace()
        try:
            resp = await client.get(url, params=params)
        except _RETRYABLE_TRANSPORT as exc:
            if attempt == 2:
                raise
            logger.info(
                "NCBI transport failure on attempt %d (%s: %s) — retrying",
                attempt + 1, type(exc).__name__, exc,
            )
            await asyncio.sleep(1.0 * (2 ** attempt))
            continue
        if resp.status_code in (429, 500, 502, 503) and attempt < 2:
            await asyncio.sleep(1.0 * (2 ** attempt))
            continue
        resp.raise_for_status()
        return resp


def _per_item_4xx(exc: BaseException) -> int | None:
    """The status of an ``httpx.HTTPStatusError`` that is a 4xx other than
    429, else None."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if 400 <= code < 500 and code != 429:
            return code
    return None


def _is_per_item_failure(exc: BaseException) -> bool:
    """Whether a failed NCBI request is a PERMANENT problem with one item.

    Only three kinds qualify, each a statement NCBI made about the request it
    was given, which a retry of the job would get back identically:

    * an ``httpx.HTTPStatusError`` with a 4xx other than 429 (``_ncbi_get``
      does not retry these);
    * a ``PubMedParseError`` — a body that arrived and is not XML/JSON;
    * a ``json.JSONDecodeError`` (or the ``UnicodeDecodeError`` ``json.loads``
      raises on undecodable bytes) from ``resp.json()`` on one body.

    Everything else is NOT per-item, and strict callers re-raise it: a
    transport failure, 429 or 5xx (transient — the job retry is the recovery),
    and any other exception, which is most likely a bug of ours
    (``AttributeError`` from a non-dict ``resp.json()``, ``KeyError``,
    ``TypeError``) or a malformed request of ours (``LocalProtocolError``,
    ``UnsupportedProtocol``). Dropping an item for one of those would thin the
    corpus silently while the job reports success. A bare ``ValueError`` is
    deliberately excluded for the same reason: only the decode subclasses are
    evidence about a body.

    A failure that is SYSTEMIC also arrives one request at a time: NCBI
    refusing every request (a revoked API key, a blocked tool id — every
    request 403), or an outage page served with 200 to every request (every
    body unparseable). So callers additionally stop at ``_SYSTEMIC_RUN``
    identical per-item failures in a row (see there and
    ``_failure_signature``).
    """
    if _per_item_4xx(exc) is not None:
        return True
    return isinstance(exc, (PubMedParseError, json.JSONDecodeError, UnicodeDecodeError))


def _failure_signature(exc: BaseException) -> tuple[str, int | str]:
    """What makes two per-item failures "the same" for ``_SYSTEMIC_RUN``: the
    status of a 4xx, or the exception type of an unreadable body. Call only on
    an exception ``_is_per_item_failure`` accepted."""
    code = _per_item_4xx(exc)
    if code is not None:
        return ("HTTP", code)
    return ("body", type(exc).__name__)


def _describe(sig: tuple[str, int | str]) -> str:
    return f"HTTP {sig[1]}" if sig[0] == "HTTP" else f"with an unreadable body ({sig[1]})"


# How many identical per-item failures in a row a strict caller accepts before
# treating them as systemic and re-raising. Three: one bad record is common,
# two in a row happen, and three consecutive identical failures (the same 4xx
# status, or the same unreadable-body error) are far likelier to be NCBI
# refusing US, or serving an outage page, than three bad items.
# ``fetch_pubmed_records`` checks the FIRST three single-PMID re-fetches of a
# fallback (they follow a batch that already failed, so they are the cheapest
# evidence available); ``convert_dois_to_pmids`` checks consecutive per-DOI
# lookups. A fallback over fewer than three PMIDs cannot reach the threshold,
# and its drops are reported through ``permanently_dropped`` instead.
_SYSTEMIC_RUN = 3


async def fetch_pubmed_records(
    pmids: list[str],
    *,
    strict: bool = False,
    permanently_dropped: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Batch fetch PubMed records for a list of PMIDs.
    Returns list of dicts with: pmid, doi, pmcid, title, abstract, journal, year,
    pub_types, authors, author_count, coi_statement.

    ``strict`` decides what one failed batch costs:

    * ``strict=False`` (the default, for ingest callers such as
      ``industry_evidence`` and the repair/sparse-data scripts): any failure is
      logged and that batch's 100 PMIDs are lost, no more — the job of those
      callers is to keep a long ingest going.
    * ``strict=True`` (``resolve_corpus``) splits on ``_is_per_item_failure``:

      - anything that is NOT a per-item failure re-raises: a transient failure
        (transport error, 429, 5xx), whose recovery is the job retry, and any
        other exception (a bug of ours). A profile corpus silently missing a
        batch is thinner than the PI's real record, and the tenure start and
        synthesis built on it would be wrong with nothing to show for it.
      - a per-item failure (a 4xx other than 429, an unparseable body) would
        repeat on every retry, so the batch's PMIDs are re-fetched one at a
        time instead. A PMID whose own fetch also fails per-item is dropped
        with one WARNING naming it; any other failure during that fallback
        re-raises, and so does the fallback's ``_SYSTEMIC_RUN``-th single
        fetch when the first ``_SYSTEMIC_RUN`` all failed the same way (the
        same 4xx status, or the same unreadable-body error): systemic, not
        per-item. A one-PMID batch skips the
        re-fetch (it would be the same request) and is dropped with the same
        WARNING.

    ``permanently_dropped``, when given, receives every PMID strict mode
    dropped, in order, so the caller can tell a complete result from one that
    is missing records a retry would not recover (``resolve_corpus`` exposes
    it as ``CorpusResult.permanently_dropped``; a tenure start is never
    persisted from such a corpus). Non-strict mode records nothing there: it
    loses whole batches for any reason, transient included.

    ``fetch_abstract`` deliberately does NOT come through here (see its
    docstring): a single-record lookup needs exactly the information the
    non-strict loop discards.
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
            if not strict:
                logger.error("Failed to fetch PubMed batch %s: %s", batch[:3], exc)
                continue
            if not _is_per_item_failure(exc):
                raise
            if len(batch) == 1:
                _drop_pmid(batch[0], exc, permanently_dropped)
                continue
            logger.warning(
                "PubMed batch %s... (%d PMIDs) failed permanently (%s: %s); "
                "re-fetching its PMIDs one at a time",
                batch[:3], len(batch), type(exc).__name__, exc,
            )
            # Signatures of the fallback's leading single fetches, while every
            # one of them has failed per-item.
            leading: list[tuple[str, int | str]] = []
            for n, pmid in enumerate(batch):
                try:
                    results.extend(await _fetch_pubmed_batch([pmid]))
                except Exception as one_exc:
                    if not _is_per_item_failure(one_exc):
                        raise
                    if len(leading) == n:
                        leading.append(_failure_signature(one_exc))
                        if (
                            len(leading) == _SYSTEMIC_RUN
                            and len(set(leading)) == 1
                        ):
                            logger.error(
                                "PubMed EFetch: the first %d single-PMID "
                                "re-fetches all failed %s; treating it as "
                                "systemic, not per-record",
                                _SYSTEMIC_RUN, _describe(leading[0]),
                            )
                            raise
                    _drop_pmid(pmid, one_exc, permanently_dropped)
    return results


def _drop_pmid(
    pmid: str, exc: BaseException, permanently_dropped: list[str] | None
) -> None:
    logger.warning(
        "PubMed EFetch for PMID %s failed permanently (%s: %s); dropping that "
        "record — a retry would fail the same way",
        pmid, type(exc).__name__, exc,
    )
    if permanently_dropped is not None:
        permanently_dropped.append(pmid)


async def fetch_authoritative_dois(pmids: list[str]) -> dict[str, str]:
    """Return ``{pmid: doi}`` — the DOI PubMed has on record for each PMID.

    Uses the esummary endpoint, whose ``articleids`` are strictly article-scoped
    (they never include the reference list), making this an authoritative source
    independent of the efetch XML parser. PMIDs with no record or no DOI on file
    are omitted. Used by the ingest gate's audit counterpart,
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


class PubMedParseError(ValueError):
    """A PubMed response that arrived and could not be read.

    Distinct from "PubMed has no such record", which is an EMPTY result and a
    real answer. This used to be swallowed into that same empty list, so
    `_fetch_pubmed_batch` returned `[]` without raising, `fetch_abstract`'s
    `_LOOKUP_FAILED` guard never fired, and the model was told "No PubMed record
    found for X" — an affirmative statement about the world — for a request that
    completed with a body we could not parse.

    A `ValueError` so the broad `except Exception` handlers that already exist
    on every long-running path keep catching it unchanged; the point is that a
    caller who needs to tell the two apart now CAN.
    """


def _parse_pubmed_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse PubMed XML efetch response.

    Returns `[]` for a well-formed response containing no articles. RAISES
    `PubMedParseError` when the body is not XML at all — see that class.
    """
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("Failed to parse PubMed XML: %s", exc)
        raise PubMedParseError(f"PubMed response was not parseable XML: {exc}") from exc

    # D13/item 7: `root.findall(".//PubmedArticle")` never matches a
    # `PubmedBookArticle` (a book/chapter record's own top-level element), so
    # a book PMID used to vanish silently — no record, no error, no count —
    # and every "resolved N, stored M" arithmetic downstream absorbed the gap
    # without a trace. Book records are NOT parsed here (their schema is
    # different enough — BookDocument, not MedlineCitation/Article — that
    # reusing this parser's xpaths would be guessing); instead they are
    # counted and logged so a caller can see the omission rather than lose it.
    book_articles = root.findall(".//PubmedBookArticle")
    if book_articles:
        book_pmids = [
            (el.text if (el := book.find(".//PMID")) is not None else "?")
            for book in book_articles
        ]
        logger.warning(
            "_parse_pubmed_xml: %d PubmedBookArticle record(s) present and "
            "NOT parsed (book/chapter PMIDs %s) — they are absent from the "
            "returned list entirely, not merely skipped; do not treat "
            "resolved-vs-stored count differences as accounting for them",
            len(book_articles), book_pmids,
        )

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

        # Title. itertext(), not .text: .text ends at the FIRST inline child
        # element, so an <i>/<sup>/<b> inside the title truncated the stored
        # field (112 production titles were repaired for this on 2026-08-13;
        # Task 1 of docs/plans/2026-08-13-pi-profile-coverage-plan.md is this
        # fix).
        title_el = article.find(".//ArticleTitle")
        record["title"] = (
            "".join(title_el.itertext()) if title_el is not None else ""
        )

        # Abstract (same truncation hazard as the title)
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

        # Authors: names, affiliations and collectives — the corpus resolver's
        # identity gates (src/services/corpus.py) need the surname/forename
        # pair, consortium detection, and the PI's OWN affiliation strings.
        authors: list[dict[str, Any]] = []
        for author in article.findall(".//Author"):
            last_el = author.find("LastName")
            fore_el = author.find("ForeName")
            init_el = author.find("Initials")
            coll_el = author.find("CollectiveName")
            affiliations = [
                text
                for aff in author.findall(".//AffiliationInfo/Affiliation")
                if (text := "".join(aff.itertext()).strip())
            ]
            authors.append(
                {
                    "last": last_el.text if last_el is not None else None,
                    "fore": fore_el.text if fore_el is not None else None,
                    "initials": init_el.text if init_el is not None else None,
                    "collective": coll_el.text if coll_el is not None else None,
                    "affiliations": affiliations,
                }
            )
        record["authors"] = authors
        record["author_count"] = len(authors)

        record["coi_statement"] = article.findtext(".//CoiStatement")

        results.append(record)

    return results


async def search_pmids(
    term: str, retmax: int = 200, sort: str = "pub date"
) -> list[str]:
    """PubMed ESearch → PMIDs, newest first.

    RAISES on transport or parse failure rather than returning ``[]`` — for
    the corpus resolver a silent empty result is a thin corpus stored as if it
    were the answer (coverage design D1/D2; audit M5 says raise and let the
    job retry).
    """
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": retmax,
        "sort": sort,
    }
    resp = await _ncbi_get(f"{EUTILS_BASE}/esearch.fcgi", params)
    try:
        data = resp.json()
    except ValueError as exc:
        raise PubMedParseError(f"ESearch response was not JSON: {exc}") from exc
    return list(data.get("esearchresult", {}).get("idlist", []))


async def convert_dois_to_pmids(
    dois: list[str],
    *,
    strict: bool = False,
    permanently_dropped: list[str] | None = None,
) -> dict[str, str]:
    """
    Convert DOIs to PMIDs. First tries NCBI ID converter (batch, but PMC-only),
    then falls back to PubMed ESearch for unresolved DOIs.
    Returns dict of {doi: pmid}, keyed by the CALLER's DOI form — every key is
    an element of ``dois``, never the resolver's own spelling of one.

    A DOI absent from the result is an ANSWER ("NCBI does not map it"): an
    idconv ``status == "error"`` record, an ESearch with other than exactly one
    hit, or a round-trip DOI mismatch. A FAILED request (idconv batch, per-DOI
    ESearch, or the round-trip EFetch) is logged and swallowed by default, which
    makes it indistinguishable from an answer. With ``strict=True``
    (``resolve_corpus``) the failure is split by ``_is_per_item_failure``:

    * NOT per-item (transport error, 429, 5xx, or any exception other than a
      4xx/unreadable body — most likely a bug of ours): re-raises, so the job
      retries or fails visibly.
    * PER-ITEM (a 4xx other than 429, an unreadable body): it would repeat on
      every retry, so it is treated as a miss, with a WARNING naming the
      DOI(s). A failed idconv batch leaves its DOIs to the ESearch phase. A
      per-DOI lookup that fails per-item — its ESearch or its round-trip
      EFetch — counts as "no PMID for this DOI" and the DOI is appended to
      ``permanently_dropped`` when that list is given; but ``_SYSTEMIC_RUN``
      consecutive per-DOI lookups failing the same way (the same 4xx status,
      or the same unreadable-body error) re-raise, because that is NCBI
      refusing or failing every request, not that many bad DOIs.

    ``permanently_dropped`` is populated in strict mode only; a DOI answered
    as unmapped (above) is an answer and is never recorded there.

    Single ESearch hits are verified in batches of at most 100 through
    ``_fetch_pubmed_batch``, matched back to their DOI by PMID (a one-candidate
    batch keeps today's ``records[0]``); a per-item failure of a batch falls back
    to one fetch per PMID with today's per-item/systemic classification, so one
    bad record loses only its own DOI. The systemic-run counter is shared by the
    ESearch and verification phases; because the phases now run one after the
    other, a verification failure is counted after all the ESearches rather than
    interleaved with them. Inside an ``ncbi_session()`` each accepted record is
    also kept for ``session_verified_records()``.
    """
    if not dois:
        return {}
    mapping: dict[str, str] = {}
    await _idconv_phase(dois, mapping, strict=strict)
    remaining = [d for d in dois if d not in mapping]
    if remaining:
        logger.info("Resolving %d remaining DOIs via PubMed ESearch", len(remaining))
        run = _Run()
        candidates = await _esearch_phase(
            remaining, run, strict=strict, permanently_dropped=permanently_dropped
        )
        await _verify_phase(
            candidates, mapping, run, strict=strict, permanently_dropped=permanently_dropped
        )
    return mapping


@dataclass
class _Run:
    """Consecutive per-DOI lookups that failed per-item the same way, and that
    failure's signature; any other outcome resets it."""

    n: int = 0
    sig: tuple[str, int | str] | None = None

    def reset(self) -> None:
        self.n, self.sig = 0, None

    def note_failure(
        self, exc: BaseException, doi: str, permanently_dropped: list[str] | None
    ) -> None:
        """Strict mode: re-raise anything but a per-item failure (and a systemic run
        of them); otherwise report the DOI as dropped. Call inside an ``except``."""
        if not _is_per_item_failure(exc):
            raise exc
        sig = _failure_signature(exc)
        self.n, self.sig = (self.n + 1, sig) if sig == self.sig else (1, sig)
        if self.n >= _SYSTEMIC_RUN:
            logger.error(
                "DOI lookup: %d consecutive DOIs failed %s; treating "
                "it as systemic, not per-DOI",
                self.n, _describe(sig),
            )
            raise exc
        logger.warning(
            "ESearch DOI lookup for %s failed permanently (%s: %s); "
            "treating it as no PMID for this DOI",
            doi, type(exc).__name__, exc,
        )
        if permanently_dropped is not None:
            permanently_dropped.append(doi)


async def _idconv_phase(dois: list[str], mapping: dict[str, str], *, strict: bool) -> None:
    # Phase 1: NCBI ID converter (batch — only finds PMC-indexed papers).
    # The converter echoes each DOI in ITS canonical casing (lowercased), not
    # the caller's: ORCID records often carry the publisher's uppercase form
    # (10.1039/D0RA08249J), and keying the result by the echo handed
    # resolve_corpus a mapping key its doi_pool never held — the bare KeyError
    # that killed the Konig and Slusher generate_profile jobs three attempts
    # each (2026-08-25). Fold the echo back to the requested form; an echo
    # matching no request even case-folded is dropped, and that input then
    # falls through to the ESearch phase, which round-trip-verifies before
    # accepting anything.
    for i in range(0, len(dois), 200):
        batch = dois[i : i + 200]
        requested_by_fold: dict[str, str] = {}
        for d in batch:
            requested_by_fold.setdefault(_doi_fold(d), d)
        try:
            params = {"ids": ",".join(batch), "format": "json"}
            resp = await _ncbi_get(IDCONV_BASE, params)
            data = resp.json()
            for record in data.get("records", []):
                if record.get("status") == "error":
                    continue
                doi = record.get("doi")
                pmid = record.get("pmid")
                if not (doi and pmid):
                    continue
                requested_doi = requested_by_fold.get(_doi_fold(doi))
                if requested_doi is None:
                    logger.warning(
                        "ID converter echoed DOI %r, which matches no "
                        "requested DOI even case-folded; dropping the record",
                        doi,
                    )
                    continue
                mapping[requested_doi] = str(pmid)
        except Exception as exc:
            if not strict:
                logger.warning("Failed batch DOI→PMID via ID converter: %s", exc)
                continue
            if not _is_per_item_failure(exc):
                raise
            logger.warning(
                "ID converter batch failed permanently (%s: %s); its DOIs "
                "fall through to ESearch: %s",
                type(exc).__name__, exc, ", ".join(batch),
            )


async def _esearch_phase(
    remaining: list[str],
    run: _Run,
    *,
    strict: bool,
    permanently_dropped: list[str] | None,
) -> list[tuple[str, str]]:
    """Phase 2: PubMed ESearch per DOI; returns the single-hit ``(doi, pmid)``
    candidates, still to be round-trip verified."""
    candidates: list[tuple[str, str]] = []
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
            # D4b (coverage design §7): a multi-hit DOI ESearch is a MISS,
            # not a hit. Taking idlist[0] unchecked resolved one Research
            # Square DOI to four unrelated PMIDs and stored the first —
            # the same wrong paper landed on six PIs' rows.
            if len(id_list) != 1:
                if len(id_list) > 1:
                    logger.warning(
                        "ESearch for DOI %s returned %d PMIDs; treating "
                        "as a miss (D4b)", doi, len(id_list),
                    )
            else:
                candidates.append((doi, id_list[0]))
                continue
        except Exception as exc:
            if not strict:
                logger.debug("ESearch DOI lookup failed for %s: %s", doi, exc)
                continue
            run.note_failure(exc, doi, permanently_dropped)
            continue
        run.reset()
    return candidates


async def _fetch_verification_chunk(
    chunk: list[tuple[str, str]],
    run: _Run,
    *,
    strict: bool,
    permanently_dropped: list[str] | None,
) -> dict[str, dict[str, Any]]:
    """PMID -> record for one chunk of at most 100 candidates: one batch fetch, or
    (on a per-item failure of it) one fetch per PMID. A one-candidate chunk maps its
    PMID to ``records[0]``, as the single-PMID fetch always did."""
    pmids = list(dict.fromkeys(pmid for _doi, pmid in chunk))

    def _index(records: list[dict[str, Any]], asked: list[str]) -> dict[str, dict[str, Any]]:
        if len(asked) == 1:
            return {asked[0]: records[0]} if records else {}
        return {str(r.get("pmid")): r for r in records}

    try:
        by_pmid = _index(await _fetch_pubmed_batch(pmids), pmids)
        run.reset()
        return by_pmid
    except Exception as exc:
        if strict and not _is_per_item_failure(exc):
            raise
        if not strict:
            logger.debug("verification batch failed (%s); falling back to single fetches", exc)
    by_pmid = {}
    for doi, pmid in chunk:
        try:
            by_pmid.update(_index(await _fetch_pubmed_batch([pmid]), [pmid]))
        except Exception as one:
            if not strict:
                logger.debug("ESearch DOI lookup failed for %s: %s", doi, one)
                continue
            run.note_failure(one, doi, permanently_dropped)
            continue
        run.reset()
    return by_pmid


async def _verify_phase(
    candidates: list[tuple[str, str]],
    mapping: dict[str, str],
    run: _Run,
    *,
    strict: bool,
    permanently_dropped: list[str] | None,
) -> None:
    """Round-trip verify the ESearch candidates: the PMID's authoritative DOI must
    equal the queried DOI, or the single hit is still the wrong paper."""
    verified = _VERIFIED_RECORDS.get()
    for i in range(0, len(candidates), 100):
        chunk = candidates[i:i + 100]
        by_pmid = await _fetch_verification_chunk(
            chunk, run, strict=strict, permanently_dropped=permanently_dropped
        )
        for doi, pmid in chunk:
            record = by_pmid.get(str(pmid))
            if record is None:
                continue
            authoritative = normalize_doi(record.get("doi"))
            queried = normalize_doi(doi)
            if authoritative and queried and authoritative.lower() == queried.lower():
                mapping[doi] = pmid
                if verified is not None:
                    verified[str(pmid)] = record
            else:
                logger.warning(
                    "ESearch hit for DOI %s (PMID %s) failed round-trip "
                    "verification (authoritative DOI %r); treating as a "
                    "miss (D4b)", doi, pmid, authoritative,
                )


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
    """Extract the methods/materials section text from PMC XML.

    The module's OTHER ``ET.ParseError`` handler, and it deliberately keeps
    swallowing — checked when ``_parse_pubmed_xml`` was changed to raise. The
    reason they differ is what the caller says afterwards: an empty parse there
    became "No PubMed record found for X", an affirmative claim about the world,
    whereas ``None`` here reaches ``fetch_full_text`` as ``methods: None``, which
    ``src/agent/tools.py::_format_full_text`` renders by omitting the Methods
    block entirely. Nothing is asserted, so there is nothing to correct — and
    ``fetch_pmc_methods`` wraps this in ``except Exception -> None`` anyway, so
    raising would change no observable behaviour.
    """
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


# src/agent/tools.py returns this dict's ``error`` value to the model verbatim, so
# the wording IS the claim the model acts on. "No PubMed record found for X" is an
# affirmative statement about the world; a request that never completed does not
# license it. Run 8b64a0e0 (M12) made exactly that substitution for PMID 41130592,
# a paper the hub had cited by DOI four seconds earlier.
_LOOKUP_FAILED = (
    "PubMed lookup FAILED for {ident} ({exc}) — the lookup did not complete "
    "SUCCESSFULLY after retries (the request never landed, or it came back "
    "unreadable). This is NOT evidence that no such record exists; retry before "
    "drawing any conclusion from it."
)


async def fetch_abstract(pmid_or_doi: str) -> dict[str, Any]:
    """
    Fetch a paper's abstract given a PMID or DOI.

    Returns dict with: pmid, title, abstract, journal, year (or error key).

    Three failure modes, deliberately worded differently (see ``_LOOKUP_FAILED``):
    the lookup did not complete successfully, the lookup completed and PubMed has
    no such record, and the DOI could not be resolved — which is itself either of
    the first two, because ``convert_dois_to_pmids`` swallows its own errors and
    cannot say which.

    "Did not complete successfully" covers a response that ARRIVED and would not
    parse (``PubMedParseError``), which used to come through as an empty list and
    therefore as the second message — an affirmative "No PubMed record found for
    X" about a paper nobody had actually looked up.

    Calls ``_fetch_pubmed_batch`` rather than ``fetch_pubmed_records`` on purpose:
    the batch wrapper's job is to keep a long ingest run going, so it swallows
    per-batch failures, which is exactly the information this caller needs.
    """
    pmid = pmid_or_doi.strip()

    # If it looks like a DOI, resolve to PMID first
    if "/" in pmid or pmid.startswith("10."):
        mapping = await convert_dois_to_pmids([pmid])
        resolved = mapping.get(pmid)
        if not resolved:
            return {
                "error": (
                    f"Could not resolve DOI {pmid} to a PMID — either PubMed has no "
                    f"record for it or the lookup failed (both are swallowed by the "
                    f"converter, so they are indistinguishable here). This is not "
                    f"evidence that the paper does not exist."
                )
            }
        pmid = resolved

    try:
        records = await _fetch_pubmed_batch([pmid])
    except Exception as exc:
        logger.warning("PubMed lookup failed for %s: %s", pmid, exc)
        return {"error": _LOOKUP_FAILED.format(ident=pmid, exc=type(exc).__name__)}
    if not records:
        return {"error": f"No PubMed record found for {pmid}"}

    rec = records[0]
    return {
        "pmid": rec.get("pmid", pmid),
        "title": rec.get("title", ""),
        "abstract": rec.get("abstract", ""),
        "journal": rec.get("journal", ""),
        "year": rec.get("year"),
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
