"""PubMed and PMC fetching service with rate limiting."""

import asyncio
import logging
import re
import time
import weakref
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
# traffic at 10 req/s. TWO SEPARATE MECHANISMS enforce that, and conflating them
# is how the original defect (COR-29c) arose:
#   * the semaphore below bounds CONCURRENCY — how many NCBI requests may be in
#     flight at once. It does NOT bound the aggregate rate: N slots each sleeping
#     `interval` seconds allowed up to N/interval requests per second, well past
#     the ceiling (keyless peaked at ~5.9 req/s against a 3 req/s limit).
#   * `_pace_ncbi` below bounds RATE — a monotonic-clock gate that spaces request
#     STARTS at least `interval` apart, process-wide, no matter how many callers
#     are in flight or which event loop they run on.
# The slot is now taken per ATTEMPT (via get_with_retry's `attempt_context`), not
# once around the whole retry loop, so a caller backing off after a 429 no longer
# occupies a slot while it is issuing no request at all (over-impl R4). That
# cannot raise the in-flight count above the slot count: a retry has to
# re-acquire before it may send anything.
#
# The semaphores are per EVENT LOOP, not per process (closure-23 R7). Like
# `asyncio.Lock`, an `asyncio.Semaphore` binds to a loop not at construction but
# at its first *contended* acquire — `Semaphore.acquire` only reaches
# `self._get_loop()` on the branch where it has to wait (checked against CPython
# 3.11's and 3.12's asyncio/locks.py). A module-level singleton therefore
# survives any number of `asyncio.run()` calls right up until the day one of them
# contends it, and then raises "bound to a different event loop" in every later
# loop. Keying on the running loop removes the hazard instead of documenting it,
# and lets the contract tests drop the per-test rebinding that used to paper over
# it. The keys are weak, so a finished loop's entry goes away with the loop.
_NCBI_SEMAPHORE_SIZES = {True: 8, False: 2}
_ncbi_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[bool, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _ncbi_semaphore(has_key: bool) -> asyncio.Semaphore:
    """Return the RUNNING loop's NCBI concurrency semaphore for `has_key`.

    Created on first use per (loop, has_key) pair — see the comment above for why
    a single process-wide Semaphore is a latent cross-loop failure.
    """
    loop = asyncio.get_running_loop()
    per_loop = _ncbi_semaphores.get(loop)
    if per_loop is None:
        per_loop = {}
        _ncbi_semaphores[loop] = per_loop
    sem = per_loop.get(has_key)
    if sem is None:
        sem = asyncio.Semaphore(_NCBI_SEMAPHORE_SIZES[has_key])
        per_loop[has_key] = sem
    return sem


# Seconds between request STARTS, i.e. the inverse of the aggregate rate ceiling
# `_pace_ncbi` enforces. NCBI's policy is 10 req/s keyed, 3 req/s keyless:
#   keyed   1/0.105 = 9.52 req/s = 95.2% of 10  (was 0.12 = 8.33 req/s = 83.3%;
#           over-impl R4 — throttling ourselves 17% below the ceiling costs
#           throughput on the profile pipeline and buys nothing)
#   keyless 1/0.34  = 2.94 req/s = 98.0% of 3   (unchanged — already at ceiling)
#
# CORRECTED (audit D5). The sentence that stood here — "the gate only ever DELAYS a
# start, so the achieved rate is always <= 1/interval: at or under the policy
# ceiling, never over it" — is FALSE, and it is the claim that let a 17-requests-in-
# one-second regression pass review. Two separate errors:
#
#   1. It conflates the reservation SCHEDULE with the DEPARTURES. `_pace_ncbi` hands
#      out start times at least `interval` apart, but it can only delay a start
#      relative to a loop that is actually running. Block the loop — as building an
#      `httpx.AsyncClient` per call did, ~32 ms of synchronous SSL work — and every
#      reservation that came due during the stall departs in the same tick. Measured
#      at N=30: 17 starts in one second against the keyed ceiling of 10, and 5
#      against the keyless ceiling of 3.
#   2. `1/interval` is an ASYMPTOTIC AVERAGE, not the worst case NCBI meters. The
#      most starts that fit in a 1 s window is `floor(1/interval) + 1`, because ten
#      starts spaced 0.105 s apart span only 0.945 s. So even a perfectly-running
#      loop put 10 in a second at the old interval — exactly the ceiling.
#
# What is actually true: the gate bounds the schedule, and the burst bound is
# `floor(1/interval) + 1`. Both are pinned by tests
# (`test_the_keyed_burst_bound_is_one_under_the_policy_ceiling` and
# `test_a_concurrent_burst_never_exceeds_the_ncbi_arrival_ceiling`), because pinning
# the average alone is what failed here.
#: Built ONCE at import. `httpx.AsyncClient(...)` otherwise does ~32 ms of synchronous,
#: event-loop-BLOCKING work per call (SSL context + certifi CA bundle parse) -- measured
#: here: median 31.9 ms, min 27.8, max 33.9 over 12 builds with the caches already warm;
#: with a shared context, 0.15 ms.
#:
#: That block defeats `_pace_ncbi`, which is why this is not a micro-optimisation. The gate
#: can only delay a start relative to a loop that is actually RUNNING. `asyncio.gather` puts
#: every `_ncbi_get` coroutine in the ready queue and each runs to its first real await only
#: after building its client, so the loop stalls for N x 32 ms and every reservation that
#: came due during the stall departs in the same tick. Measured before this fix: 17 starts
#: inside one second against NCBI's 10 req/s keyed ceiling at N=30 (min gap 0.15 ms), and 5
#: inside one second against the 3 req/s keyless ceiling.
#:
#: `httpx.create_ssl_context()` rather than a hand-rolled `ssl.create_default_context(...)`:
#: the two were compared and match exactly on this httpx (0.28.1) -- check_hostname=True,
#: verify_mode=CERT_REQUIRED, minimum_version=MINIMUM_SUPPORTED -- but only httpx's own
#: cannot drift away from httpx's defaults and silently weaken TLS. An `ssl.SSLContext` is
#: not an asyncio object, so unlike a shared `AsyncClient` it does not reintroduce the
#: event-loop affinity bug closure-23 R7 fixed for the semaphore (verified across two
#: successive `asyncio.run()` calls).
_NCBI_SSL_CONTEXT = httpx.create_ssl_context()

#: Keyed: 0.112 s, not the 0.105 this shipped with. NCBI meters ARRIVALS per second, and the
#: worst-case number of starts a gate spacing them `interval` apart admits inside a 1 s
#: window is `floor(1/interval) + 1` -- ten starts at 0.105 s span 0.945 s and all land in
#: the same second. So 0.105 sat at exactly 10, the ceiling itself, with no tolerance for
#: any jitter between our start and NCBI's arrival. 0.112 > 1/9 puts the worst case at 9,
#: one request of margin, for 8.93 req/s average (89.3% of the granted ceiling -- still well
#: above the 83.3% that over-impl R4 / closure-23 R2 filed as wasteful).
#:
#: Keyless stays at 0.34 (2.94 req/s, worst-case burst 3 = its ceiling). Buying the same
#: one-request margin there needs interval > 0.5 s, i.e. a third of the throughput, on a
#: fallback path production does not use -- so that path is held AT its ceiling rather than
#: under it, and the asymmetry is deliberate.
_NCBI_PACING_SECONDS = {True: 0.112, False: 0.34}

# Total retry budget for ONE logical `_ncbi_get`, in seconds (over-impl R3: the
# retry loop shipped with four attempts and no total ceiling). 120 s = 2x the 60 s
# client timeout below, so a fully hung NCBI spends two attempts (t=0 and t=60.5,
# with the 0.5 s backoff) instead of four, and a `Retry-After: 60` storm two
# instead of four. `get_with_retry` never cancels an attempt already in flight, so
# the honest worst case for one logical call is deadline + one client timeout =
# 180 s, down from 420 s — see `_out_of_budget` in http_retry.py. This bounds each
# CALL, not the pipeline: `convert_dois_to_pmids` still issues one call per
# unresolved DOI, so the aggregate is 180 s x DOI count in the worst case.
_NCBI_RETRY_DEADLINE_SECONDS = 120.0

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
# race it, and the cursor itself is never bound to any loop at all — which is why
# it stays a single process-wide value while the semaphores above have to be
# rebuilt per loop (closure-23 R7), and why the RATE ceiling still holds
# process-wide even though the CONCURRENCY bound is now per loop.
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
    (COR-29b) happens via ``_pace_ncbi``, passed in as ``get_with_retry``'s ``before_request`` hook
    rather than called once here directly (issue #23 I1) — the hook fires at the top of EVERY loop
    iteration, including the first, so every attempt (not just the initial request) reserves a
    pacing slot. This replaces, rather than supplements, the old single pre-call
    ``await _pace_ncbi(...)``: keeping both would reserve twice for the first attempt. A retry's own
    exponential backoff no longer has to out-run the pacing interval on its own — the gate now
    applies uniformly, so a burst of 429s can't re-fire faster than NCBI's ceiling the way it could
    when only the first attempt was paced (measured: 16-25 req/s against a 10 req/s ceiling under
    concurrency).

    The concurrency slot is likewise passed in as ``attempt_context`` rather than wrapped around
    the whole call, so it is held for one attempt and released across the backoff (over-impl R4),
    and it is resolved per attempt through ``_ncbi_semaphore`` so it belongs to the loop actually
    running (closure-23 R7). ``deadline`` caps the total retry budget (over-impl R3).
    """
    settings = get_settings()
    has_key = bool(settings.ncbi_api_key)
    if has_key:
        params["api_key"] = settings.ncbi_api_key
    params.setdefault("tool", _NCBI_TOOL)
    params.setdefault("email", settings.ncbi_contact_email or settings.ses_sender_email)
    async with httpx.AsyncClient(
        timeout=60, follow_redirects=True, verify=_NCBI_SSL_CONTEXT
    ) as client:
        return await get_with_retry(
            client,
            url,
            params=params,
            backoff=_RETRY_BACKOFF,
            before_request=lambda: _pace_ncbi(_NCBI_PACING_SECONDS[has_key]),
            attempt_context=lambda: _ncbi_semaphore(has_key),
            deadline=_NCBI_RETRY_DEADLINE_SECONDS,
        )


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
