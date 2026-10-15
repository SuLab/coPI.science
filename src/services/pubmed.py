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
from src.services import pmc_methods
from src.services.http_pacing import Pacer
from src.services.identifiers import normalize_doi, normalize_orcid
from src.services.secret_redaction import install_log_redaction, redact_api_keys

logger = logging.getLogger(__name__)

# httpx's request lines and this module's own lines are redacted by a logger filter,
# and every other record by the log-record factory, traceback text included
# (src/services/secret_redaction.py). Every process that holds the NCBI key imports
# this module: the app (pi_profile), the worker (profile_pipeline) and the agent (tools).
install_log_redaction("httpx", __name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# The trailing slash is load-bearing: without it NCBI answers 301 to the same path
# WITH one (live-verified 2026-08-22), so every lookup cost two requests. That was
# 388 requests for 194 lookups in run 8b64a0e0 — 32% of all our NCBI traffic — and
# because _pace() only counts the request it issues, the real rate against NCBI was
# twice what the pacer believed.
IDCONV_BASE = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
# DOI query strings are much larger than PMID lists. Smaller converter batches
# avoid repeated 429 responses observed for large DOI requests, without treating
# transport failures as evidence that the papers do not exist.
IDCONV_DOI_BATCH_SIZE = 100

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


@asynccontextmanager
async def ncbi_session() -> AsyncIterator[httpx.AsyncClient]:
    """One httpx client for every NCBI request issued inside the block (DP-06):
    `resolve_corpus` wraps its retrieval in this, so a corpus reuses one connection
    pool instead of opening a client per request. Pacing and the semaphore are
    unchanged."""
    async with _make_client() as client:
        token = _SHARED_CLIENT.set(client)
        try:
            yield client
        finally:
            _SHARED_CLIENT.reset(token)


async def _ncbi_get(url: str, params: dict[str, Any]) -> httpx.Response:
    """Make a rate-limited, identified GET request to NCBI, with retry.

    Pacing runs BEFORE raise_for_status — the old order skipped pacing
    exactly when NCBI was already 429-ing us. Retries cover the transient
    statuses NCBI actually emits under load *and* the transport failures in
    ``_RETRYABLE_TRANSPORT``; anything else raises as before. The caller's ``params``
    dict is copied, never mutated, so it never carries the key.
    """
    settings = get_settings()
    query = dict(params)
    if settings.ncbi_api_key:
        query["api_key"] = settings.ncbi_api_key
    query.setdefault("tool", _NCBI_TOOL)
    query.setdefault("email", settings.ncbi_contact_email or settings.ses_sender_email)
    async with _request_semaphore:
        shared = _SHARED_CLIENT.get()
        if shared is not None:
            return await _ncbi_attempts(shared, url, query)
        async with _make_client() as client:
            return await _ncbi_attempts(client, url, query)


def _redacted_request(request: httpx.Request) -> httpx.Request:
    """A bare copy of ``request`` (method and redacted URL; no headers, no body)."""
    return httpx.Request(request.method, redact_api_keys(str(request.url)))


def _redacted_http_error(exc: httpx.HTTPError) -> httpx.HTTPError:
    """``exc`` rebuilt with no key in its message, request or response. The class is
    kept, so ``_RETRYABLE_TRANSPORT`` and every caller's ``except`` still match; a
    status error keeps only its status (``_per_item_4xx`` reads it)."""
    message = redact_api_keys(str(exc))
    try:
        req: httpx.Request | None = _redacted_request(exc.request)
    except RuntimeError:  # httpx raises it when no request is attached
        req = None
    if isinstance(exc, httpx.HTTPStatusError):
        return httpx.HTTPStatusError(
            message, request=req,
            response=httpx.Response(exc.response.status_code, request=req),
        )
    try:
        return type(exc)(message, request=req)
    except TypeError:
        if isinstance(exc, httpx.TransportError):
            return httpx.TransportError(message, request=req)
        return httpx.RequestError(message, request=req)


async def _ncbi_attempts(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> httpx.Response:
    """The paced, retried GET loop of `_ncbi_get`, on a given client.

    Invariant: an ``httpx.HTTPError`` leaving this function (so ``_ncbi_get``) carries
    no key in its ``str``, ``repr``, ``.request``, ``.response.request``, ``__cause__``
    or ``__context__``: it is rebuilt by ``_redacted_http_error`` inside the ``except``
    and raised after the block ``from None``, so its context is never the original.
    httpx exceptions outside ``HTTPError`` (``InvalidURL``, ``StreamError``) are not
    rebuilt: NCBI URLs are module constants and params are percent-encoded. Frame
    locals (``query`` in ``_ncbi_get``) are not redacted; no locals-capturing
    reporter is configured.
    """
    for attempt in range(3):
        await _pace()
        failure: httpx.HTTPError | None = None
        retryable = False
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            failure = _redacted_http_error(exc)
            retryable = isinstance(exc, _RETRYABLE_TRANSPORT)
        if failure is not None:
            if retryable and attempt < 2:
                logger.info(
                    "NCBI transport failure on attempt %d (%s: %s) — retrying",
                    attempt + 1, type(failure).__name__, failure,
                )
                await asyncio.sleep(1.0 * (2 ** attempt))
                continue
            raise failure from None
        if resp.status_code in (429, 500, 502, 503) and attempt < 2:
            await asyncio.sleep(1.0 * (2 ** attempt))
            continue
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            failure = _redacted_http_error(exc)
        if failure is not None:
            raise failure from None
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

#: PMIDs per EFetch request, for the corpus fetch and DOI verification alike.
EFETCH_BATCH_SIZE = 100


def _missing_pmids(requested: list[str], records: list[dict[str, Any]]) -> list[str]:
    """The requested PMIDs a batch that returned has no record for: deleted, book or
    invalid ids. An answer, not a failure (R2.10)."""
    returned = {str(r.get("pmid")) for r in records}
    return [p for p in (str(x).strip() for x in requested) if p not in returned]


def _log_efetch_summary(
    requested: int, failed_pmids: int, failed_batches: int, batches: int, missing: list[str],
) -> None:
    """The run's two summary lines (R2.10, main PM-6)."""
    if failed_batches:
        logger.error(
            "PubMed EFetch: %d of %d PMIDs not fetched (%d of %d batches failed)",
            failed_pmids, requested, failed_batches, batches,
        )
    if missing:
        logger.warning(
            "PubMed EFetch: %d of %d requested PMIDs came back with no record "
            "(deleted, book or invalid ids; first: %s)",
            len(missing), requested, ", ".join(missing[:10]),
        )


async def fetch_pubmed_records(
    pmids: list[str],
    *,
    strict: bool = False,
    permanently_dropped: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Batch fetch PubMed records for a list of PMIDs.
    Returns list of dicts with: pmid, doi, pmcid, title, abstract, journal, year,
    pub_types, authors, author_count, author_list_complete, coi_statement, updated_in,
    update_of. ``title`` and ``journal`` are one line. ``abstract`` is the article's own
    Abstract, else its first English OtherAbstract. Each ``authors`` entry, in AuthorList
    order, has exactly: last, fore, initials, collective, affiliations (whitespace
    collapsed, an e-mail address reduced to its domain), orcid (hyphenated
    ``0000-0000-0000-000X``, or None when absent or not a valid iD), position (1-based,
    every Author counted), suffix, valid (False exactly when ``ValidYN="N"``) and
    equal_contrib. ``author_list_complete`` is False exactly when the AuthorList carries
    ``CompleteYN="N"`` (True with no AuthorList; contract C11, read with
    ``.get(key, True)``). ``updated_in`` / ``update_of`` are PMID lists from the record's
    UpdateIn / UpdateOf CommentsCorrections links (empty when none).

    ``strict`` decides what one failed batch costs:

    * ``strict=False`` (the default, for ingest callers such as the
      repair/sparse-data scripts): any failure is logged and that batch's 100
      PMIDs are lost, no more — the job of those callers is to keep a long
      ingest going.
    * ``strict=True`` (``resolve_corpus``, and ``industry_evidence``'s PubMed COI
      source, which turns a raise into "source unavailable, keep its stored rows")
      splits on ``_is_per_item_failure``:

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

    After the loop, one ERROR sums the PMIDs of failed non-strict batches, and one WARNING
    names (at most ten of) the requested PMIDs a returned batch had no record for; a missing
    PMID is an answer, never a drop.

    ``fetch_abstract`` deliberately does NOT come through here (see its
    docstring): a single-record lookup needs exactly the information the
    non-strict loop discards.
    """
    if not pmids:
        return []

    results = []
    failed_pmids = failed_batches = 0
    missing: list[str] = []
    for i in range(0, len(pmids), EFETCH_BATCH_SIZE):
        batch = pmids[i : i + EFETCH_BATCH_SIZE]
        try:
            records = await _fetch_pubmed_batch(batch)
            results.extend(records)
            missing.extend(_missing_pmids(batch, records))
        except Exception as exc:
            if not strict:
                logger.error("Failed to fetch PubMed batch %s: %s", batch[:3], exc)
                failed_batches += 1
                failed_pmids += len(batch)
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
                    one = await _fetch_pubmed_batch([pmid])
                    results.extend(one)
                    missing.extend(_missing_pmids([pmid], one))
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
    _log_efetch_summary(
        len(pmids), failed_pmids, failed_batches, -(-len(pmids) // EFETCH_BATCH_SIZE), missing
    )
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


def _author_orcid(author: ET.Element) -> str | None:
    """The first of the author's ``Identifier Source="ORCID"`` children that is a valid iD
    (``identifiers.normalize_orcid``: check digit verified, hyphenless, single-spaced and
    orcid.org URL forms accepted), else None."""
    for ident in author.findall("Identifier"):
        if ident.get("Source") == "ORCID" and (orcid := normalize_orcid(ident.text)):
            return orcid
    return None


def _linked_pmids(article: ET.Element, ref_type: str) -> list[str]:
    """PMIDs of the record's ``CommentsCorrections`` links of ``ref_type`` ("UpdateIn",
    "UpdateOf"), in document order, without duplicates."""
    linked: list[str] = []
    for cc in article.findall("./MedlineCitation/CommentsCorrectionsList/CommentsCorrections"):
        if cc.get("RefType") != ref_type:
            continue
        linked_pmid = (cc.findtext("PMID") or "").strip()
        if linked_pmid and linked_pmid not in linked:
            linked.append(linked_pmid)
    return linked


#: An e-mail address in an affiliation (F15): replaced by its domain, so the personal part
#: is dropped and the institutional evidence ("jhmi.edu", which jhu_rules matches) stays.
_EMAIL_ADDRESS = re.compile(
    r"\s*(?:Electronic address:\s*)?[\w.+-]+@(?P<domain>[\w-]+(?:\.[\w-]+)+)\.?"
)
#: The first plausible year in a MedlineDate ("Winter 1998-1999", "Dec 1998-Jan 1999").
_YEAR_IN_TEXT = re.compile(r"(1[89]|20)\d{2}")


def _one_line(text: str | None) -> str | None:
    """``text`` with every run of whitespace (CR, LF, VT, FF, U+0085, U+2028, U+2029, tabs,
    Unicode spaces) collapsed to one space; None for anything blank or not text. The same rule
    as ``non_pubmed_works._one_line`` (Q18 ingest half)."""
    return " ".join(text.split()) if isinstance(text, str) and text.strip() else None


def _affiliation_text(raw: str) -> str:
    """One affiliation: whitespace collapsed, each e-mail address replaced by its domain
    (F15), trailing spaces and periods removed. "" when nothing is left."""
    text = " ".join(raw.split())
    text = " ".join(_EMAIL_ADDRESS.sub(lambda m: " " + m.group("domain"), text).split())
    return text.rstrip(" .")


def _author_entry(author: ET.Element, position: int) -> dict[str, Any]:
    """One ``record["authors"]`` entry (R2.6). ``valid`` and the record's
    ``author_list_complete`` are contract C11; read them with ``.get(key, True)``."""
    last_el = author.find("LastName")
    fore_el = author.find("ForeName")
    init_el = author.find("Initials")
    coll_el = author.find("CollectiveName")
    suffix_el = author.find("Suffix")
    return {
        "last": last_el.text if last_el is not None else None,
        "fore": fore_el.text if fore_el is not None else None,
        "initials": init_el.text if init_el is not None else None,
        "collective": coll_el.text if coll_el is not None else None,
        "affiliations": [
            text
            for aff in author.findall("AffiliationInfo/Affiliation")
            if (text := _affiliation_text("".join(aff.itertext())))
        ],
        "orcid": _author_orcid(author),
        "position": position,
        "suffix": suffix_el.text if suffix_el is not None else None,
        "valid": author.get("ValidYN") != "N",
        "equal_contrib": author.get("EqualContrib") == "Y",
    }


def _authors(article: ET.Element) -> tuple[list[dict[str, Any]], bool]:
    """(authors, author_list_complete) from ``MedlineCitation/Article/AuthorList``. Every
    ``Author`` is listed and numbered, collectives and ``ValidYN="N"`` included. The list is
    complete unless it carries ``CompleteYN="N"``; with no AuthorList it is complete (C11:
    Phase 3 keeps an anchored record whose list is incomplete, so a record with no list must
    stay ``no_match`` rather than be stored with no author evidence)."""
    author_list = article.find("./MedlineCitation/Article/AuthorList")
    if author_list is None:
        return [], True
    authors = [
        _author_entry(author, position)
        for position, author in enumerate(author_list.findall("Author"), start=1)
    ]
    return authors, author_list.get("CompleteYN") != "N"


def _abstract_text(container: ET.Element) -> str:
    """The ``AbstractText`` children of an Abstract or OtherAbstract, each "LABEL: text" when
    labelled, joined by spaces (itertext: inline markup must not truncate)."""
    parts = []
    for el in container.findall("AbstractText"):
        text = "".join(el.itertext())
        label = el.get("Label")
        parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)


def _abstract(article: ET.Element) -> str:
    """The article's own Abstract (R2.7, main PM-4); only when that is empty, the first
    English (or language-less) OtherAbstract with text; else "". An OtherAbstract is never
    concatenated onto the article's own."""
    own = article.find("./MedlineCitation/Article/Abstract")
    text = _abstract_text(own) if own is not None else ""
    if text:
        return text
    for other in article.findall("./MedlineCitation/OtherAbstract"):
        if (other.get("Language") or "eng").lower() == "eng" and (text := _abstract_text(other)):
            return text
    return ""


def _int_year(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _year(article: ET.Element) -> int | None:
    """PubDate/Year, else the first plausible year in PubDate/MedlineDate, else the
    electronic ArticleDate/Year (R2.8, main PM-5); a junk value falls through."""
    year = _int_year(article.findtext(".//PubDate/Year"))
    if year is None:
        match = _YEAR_IN_TEXT.search(article.findtext(".//PubDate/MedlineDate") or "")
        year = int(match.group(0)) if match else None
    if year is None:
        year = _int_year(article.findtext("./MedlineCitation/Article/ArticleDate/Year"))
    return year


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
        # fix). Collapsed to one line at ingest (Q18), so no stored title carries a
        # line break.
        title_el = article.find(".//ArticleTitle")
        record["title"] = (
            _one_line("".join(title_el.itertext())) if title_el is not None else None
        ) or ""

        # Abstract: the article's own; an OtherAbstract only as an English fallback (R2.7).
        record["abstract"] = _abstract(article)

        # Journal
        journal_el = article.find(".//Journal/Title")
        record["journal"] = _one_line(journal_el.text) if journal_el is not None else None

        # Year: PubDate/Year, MedlineDate's first year, then ArticleDate (R2.8). Absent
        # when none gives one.
        year = _year(article)
        if year is not None:
            record["year"] = year

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
        record["authors"], record["author_list_complete"] = _authors(article)
        # Invalid (ValidYN="N") authors still count, as before.
        record["author_count"] = len(record["authors"])

        record["coi_statement"] = article.findtext(".//CoiStatement")

        record["updated_in"] = _linked_pmids(article, "UpdateIn")
        record["update_of"] = _linked_pmids(article, "UpdateOf")

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

    Each input is normalised (``identifiers.normalize_doi``) first: a non-DOI is neither
    queried, mapped nor dropped; inputs that differ only in case or prefix share one query,
    and every input maps to (or is dropped with) its query. For inputs already normalised
    and unique the requests, the mapping order and the drops are exactly those of the
    per-DOI resolver (Freeze B24).

    ``permanently_dropped`` is populated in strict mode only; a DOI answered
    as unmapped (above) is an answer and is never recorded there.

    Single ESearch hits are verified in batches of at most 100 through
    ``_fetch_pubmed_batch`` (``_VerificationFetcher``), matched back to their DOI
    by PMID. Every case the batch cannot answer exactly as the old one-fetch-per-hit
    loop did gets that loop's own single fetch and its ``records[0]``: a PMID the
    batch did not return under its own number, every PMID of a batch that returned
    a duplicate or unrequested PMID, every PMID of a batch that failed per-item (or
    failed at all, non-strict), and a one-PMID chunk. So one bad record loses only
    its own DOI. All ESearches run before any verification, but every outcome is
    then classified in DOI order through one systemic-run counter, so the mapping
    (and its order), ``permanently_dropped`` (and its order) and the point of a
    systemic raise are those of the per-DOI loop.
    """
    if not dois:
        return {}
    # One query per DOI, normalised and case-folded; the caller's inputs stay the keys (R2.11).
    queries: dict[str, str] = {}
    inputs: dict[str, list[str]] = {}
    for doi in dois:
        normalized = normalize_doi(doi)
        if normalized is None:
            logger.debug("convert_dois_to_pmids: %r is not a DOI; not queried", doi)
            continue
        query = queries.setdefault(normalized.casefold(), normalized)
        inputs.setdefault(query, []).append(doi)
    unique = list(queries.values())
    resolved: dict[str, str] = {}
    dropped: list[str] = []
    try:
        if unique:
            await _idconv_phase(unique, resolved, strict=strict)
            remaining = [q for q in unique if q not in resolved]
            if remaining:
                logger.info("Resolving %d remaining DOIs via PubMed ESearch", len(remaining))
                answers = await _esearch_phase(remaining, strict=strict)
                await _verify_phase(
                    remaining, answers, resolved, strict=strict, permanently_dropped=dropped
                )
    finally:
        # A systemic raise still reports what was dropped before it, in drop order.
        if permanently_dropped is not None:
            for query in dropped:
                permanently_dropped.extend(inputs[query])
    return {doi: pmid for query, pmid in resolved.items() for doi in inputs[query]}


@dataclass
class _Run:
    """Consecutive per-DOI lookups that failed per-item the same way, and that
    failure's signature; any other outcome resets it."""

    n: int = 0
    sig: tuple[str, int | str] | None = None

    def reset(self) -> None:
        self.n, self.sig = 0, None

    def counts_to_systemic(self, exc: BaseException) -> bool:
        """Count a per-item failure into the run; True once the run is systemic."""
        sig = _failure_signature(exc)
        self.n, self.sig = (self.n + 1, sig) if sig == self.sig else (1, sig)
        return self.n >= _SYSTEMIC_RUN

    def note_failure(
        self, exc: BaseException, doi: str, permanently_dropped: list[str] | None
    ) -> None:
        """Strict mode: re-raise ``exc`` unless it is a per-item failure that does
        not complete a systemic run; otherwise report the DOI as dropped."""
        if not _is_per_item_failure(exc):
            raise exc
        if self.counts_to_systemic(exc):
            logger.error(
                "DOI lookup: %d consecutive DOIs failed %s; treating "
                "it as systemic, not per-DOI",
                self.n, _describe(self.sig),
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
    for i in range(0, len(dois), IDCONV_DOI_BATCH_SIZE):
        batch = dois[i : i + IDCONV_DOI_BATCH_SIZE]
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


async def _esearch_phase(remaining: list[str], *, strict: bool) -> list[str | BaseException | None]:
    """Phase 2: one PubMed ESearch per DOI, in order. Each answer is the single-hit
    PMID (still to be verified), None (no hit, several hits, or a non-strict
    failure), or in strict mode the failure itself, which ``_verify_phase``
    classifies in DOI order. The phase ends early at a strict failure that is not
    per-item, and at the ``_SYSTEMIC_RUN``-th identical per-item failure in a row:
    the classification raises at or before that answer, so no later one is read."""
    answers: list[str | BaseException | None] = []
    failed: list[str] = []
    streak = _Run()
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
        except Exception as exc:
            if not strict:
                logger.debug("ESearch DOI lookup failed for %s: %s", doi, exc)
                failed.append(doi)
                answers.append(None)
                continue
            answers.append(exc)
            if not _is_per_item_failure(exc) or streak.counts_to_systemic(exc):
                break
            continue
        streak.reset()
        # D4b (coverage design §7): a multi-hit DOI ESearch is a MISS,
        # not a hit. Taking idlist[0] unchecked resolved one Research
        # Square DOI to four unrelated PMIDs and stored the first —
        # the same wrong paper landed on six PIs' rows.
        if len(id_list) == 1:
            answers.append(id_list[0])
            continue
        if len(id_list) > 1:
            logger.warning(
                "ESearch for DOI %s returned %d PMIDs; treating "
                "as a miss (D4b)", doi, len(id_list),
            )
        answers.append(None)
    if failed:
        logger.warning(
            "ESearch DOI lookup failed for %d of %d DOIs (first: %s)",
            len(failed), len(remaining), failed[0],
        )
    return answers


class _VerificationFetcher:
    """The EFetch half of DOI verification: the hit PMIDs, in hit order, in chunks
    of at most ``EFETCH_BATCH_SIZE``, one batch request per chunk, made when the first hit of the
    chunk is verified. ``records(i)`` returns what the old per-hit fetch returned
    for hit ``i`` (the caller reads ``records[0]``): the batch's record under that
    PMID when the batch is unambiguous, otherwise that old single fetch itself."""

    def __init__(self, pmids: list[str], *, strict: bool) -> None:
        self._pmids = pmids
        self._strict = strict
        # chunk start -> PMID -> record; None: single fetches for that chunk.
        self._chunks: dict[int, dict[str, dict[str, Any]] | None] = {}

    async def records(self, i: int) -> list[dict[str, Any]]:
        start = i - i % EFETCH_BATCH_SIZE
        if start not in self._chunks:
            self._chunks[start] = await self._batch(self._pmids[start:start + EFETCH_BATCH_SIZE])
        by_pmid = self._chunks[start]
        pmid = self._pmids[i]
        if by_pmid is not None and pmid in by_pmid:
            return [by_pmid[pmid]]
        return await self._single(pmid)

    async def _batch(self, pmids: list[str]) -> dict[str, dict[str, Any]] | None:
        unique = list(dict.fromkeys(pmids))
        if len(unique) == 1:
            return None  # the batch request would be the single fetch itself
        try:
            records = await _fetch_pubmed_batch(unique)
        except Exception as exc:
            if self._strict and not _is_per_item_failure(exc):
                raise
            logger.debug("verification batch failed (%s); falling back to single fetches", exc)
            return None
        returned = [str(r.get("pmid")) for r in records]
        if len(set(returned)) != len(returned) or not set(returned) <= set(unique):
            # A record under a duplicate or unrequested PMID: which request it
            # answers is unknown, so every PMID of the chunk is fetched alone.
            return None
        return dict(zip(returned, records, strict=True))

    async def _single(self, pmid: str) -> list[dict[str, Any]]:
        # Strict fetches the one record directly, so a failure reaches the
        # classification with its status intact (through fetch_pubmed_records a
        # one-PMID batch would be dropped inside it and read as a verification miss).
        if self._strict:
            return await _fetch_pubmed_batch([pmid])
        return await fetch_pubmed_records([pmid])


async def _verify_phase(
    remaining: list[str],
    answers: list[str | BaseException | None],
    mapping: dict[str, str],
    *,
    strict: bool,
    permanently_dropped: list[str] | None,
) -> None:
    """Phase 3, in DOI order like the old per-DOI loop: round-trip verify each
    single hit (the PMID's authoritative DOI must equal the queried DOI, or the
    single hit is still the wrong paper) and classify each strict failure of
    either phase through one systemic-run counter. ``answers`` may stop short of
    ``remaining`` (see ``_esearch_phase``); its last answer then raises here."""
    fetcher = _VerificationFetcher([a for a in answers if isinstance(a, str)], strict=strict)
    run = _Run()
    hit = 0
    for doi, answer in zip(remaining[: len(answers)], answers, strict=True):
        if isinstance(answer, BaseException):
            run.note_failure(answer, doi, permanently_dropped)
            continue
        if answer is not None:
            hit += 1
            try:
                records = await fetcher.records(hit - 1)
            except Exception as exc:
                run.note_failure(exc, doi, permanently_dropped)
                continue
            authoritative = normalize_doi(records[0].get("doi")) if records else None
            queried = normalize_doi(doi)
            if authoritative and queried and authoritative.lower() == queried.lower():
                mapping[doi] = answer
            else:
                logger.warning(
                    "ESearch hit for DOI %s (PMID %s) failed round-trip "
                    "verification (authoritative DOI %r); treating as a "
                    "miss (D4b)", doi, answer, authoritative,
                )
        run.reset()


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
    Returns extracted methods text or None if not available. The section is located and
    serialised by ``pmc_methods.extract_methods_section`` (R2.12).
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
        return pmc_methods.extract_methods_section(resp.text)
    except Exception as exc:
        logger.debug("Failed to fetch PMC full text for %s: %s", pmcid, exc)
        return None


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


async def _openalex_abstract(doi: str) -> dict[str, Any] | None:
    """A DOI PubMed cannot resolve, looked up in OpenAlex (spec 2026-10-09 §5.6): the
    non-PubMed papers a PI's persona lists (a PubMed paper the converter missed keeps
    OpenAlex's PMID). None when OpenAlex has no such work, gives it no title, marks it
    retracted (the stored corpus skips those too), or the lookup fails; the caller then
    reports the PubMed miss exactly as before, since the prompt-freeze golden pins that
    wording. One attempt with a short timeout: the agent waits on this inside its turn."""
    # Imported here so this NCBI module does not load the OpenAlex client (and its budget
    # and job-queue imports) unless a DOI actually misses.
    from src.services import openalex

    try:
        work = await openalex.fetch_work_by_doi(
            normalize_doi(doi) or doi, attempts=1, timeout=10.0
        )
    except Exception as exc:  # best effort: the PubMed answer stands
        logger.info("OpenAlex lookup for DOI %s failed: %s", doi, type(exc).__name__)
        return None
    if not work or not work.get("title") or work.get("is_retracted"):
        return None
    return {
        "pmid": work.get("pmid"),
        "doi": normalize_doi(doi),
        "title": work["title"],
        "abstract": work.get("abstract") or "",
        "journal": work.get("venue") or "",
        "year": work.get("year"),
    }


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
            found = await _openalex_abstract(pmid)
            if found is not None:
                return found
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

    pmid = abstract_data.get("pmid")
    if not pmid:
        # A paper OpenAlex answered for (spec 2026-10-09 §5.6): PMC holds only PubMed papers.
        return {
            **abstract_data,
            "pmcid": None,
            "methods": None,
            "note": "Paper is not in PubMed, so PubMed Central has no full text for it",
        }

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
