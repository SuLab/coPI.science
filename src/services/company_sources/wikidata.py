"""Founder claims from Wikidata by ORCID (spec §7.5 Step 1b, O11).

One SPARQL query per PI against the public endpoint: the item whose ORCID iD (P496)
is the PI's, and every item whose "founded by" (P112) is that item. Verified against
the live endpoint on 2026-10-02 (ORCID 0000-0002-7086-765X returns three P112 items;
Velculescu's 0000-0003-1195-438X returns his item Q7926425 and no P112 item).

Wikimedia's User-Agent policy wants `<client>/<version> (<contact>) <library>/<version>`;
the contact is the one the operator put in `SEC_USER_AGENT` (spec §7.6), and with no
contact the lookup is not made. The query service answers 429 with `Retry-After` when
a client exceeds its limits: one paced retry after that delay, then the source is
unavailable for this run. A delay over `RETRY_AFTER_CAP_SECONDS` makes the source
unavailable at once rather than retrying early. Redirects are not followed, so a 3xx is
a failure like any non-200 answer, and an answer over `MAX_RESPONSE_BYTES` is abandoned
without being read to the end.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from src.services.company_sources import SourceUnavailable
from src.services.http_pacing import Pacer

SPARQL_URL = "https://query.wikidata.org/sparql"
CLIENT_NAME = "copi-science-company-discovery"
CLIENT_VERSION = "1.0"

# The ORCID is validated before it is interpolated, so the query text cannot be steered.
_ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_ENTITY = re.compile(r"^https?://www\.wikidata\.org/entity/(Q\d+)$")
_BARE_QID = re.compile(r"^Q\d+$")
_QUERY = (
    "SELECT ?pi ?company ?companyLabel WHERE {{\n"
    '  ?pi wdt:P496 "{orcid}" .\n'
    "  OPTIONAL {{ ?company wdt:P112 ?pi . }}\n"
    '  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}\n'
    "}}"
)
RETRY_AFTER_CAP_SECONDS = 60.0
#: Largest answer read; a bigger body makes the source unavailable.
MAX_RESPONSE_BYTES = 2_000_000
RETRY_AFTER_DEFAULT_SECONDS = 5.0

# Module-level alias so tests can patch the Retry-After sleep without patching asyncio.
_sleep = asyncio.sleep


def _interval() -> float:
    """One request per second (spec §7.5); a function so tests can zero it."""
    return 1.0


_PACER = Pacer(lambda: _interval())


@dataclass(frozen=True)
class WikidataCompany:
    company_name: str
    item: str
    url: str


@dataclass(frozen=True)
class WikidataResult:
    pi_item: str | None
    companies: list[WikidataCompany]


def contact_from(sec_user_agent: str) -> str | None:
    """The contact part of `SEC_USER_AGENT` ("Blackbird Labs admin@x.org" ->
    "admin@x.org"); the whole value when it holds no address; None when unset."""
    value = (sec_user_agent or "").strip()
    if not value:
        return None
    m = re.search(r"[^\s<>()]+@[^\s<>()]+", value)
    return m.group(0) if m else value


def user_agent(contact: str) -> str:
    return f"{CLIENT_NAME}/{CLIENT_VERSION} ({contact}) httpx/{httpx.__version__}"


def item_url(qid: str) -> str:
    return f"https://www.wikidata.org/wiki/{qid}"


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam for tests."""
    return httpx.AsyncClient(timeout=30, follow_redirects=False)


def _retry_after_seconds(value: str | None) -> float:
    """The `Retry-After` delay in seconds, uncapped (the caller refuses one over
    `RETRY_AFTER_CAP_SECONDS`); the default when absent or unreadable, 0 when past."""
    if not value:
        return RETRY_AFTER_DEFAULT_SECONDS
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_SECONDS
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


async def _read_capped(resp: httpx.Response, cap: int) -> bytes:
    """The streamed body, or `SourceUnavailable` as soon as it exceeds `cap` bytes
    (declared Content-Length or bytes received), without reading the rest."""
    declared = (resp.headers.get("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > cap:
        raise SourceUnavailable("response too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > cap:
            raise SourceUnavailable("response too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _attempt(
    client: httpx.AsyncClient, params: dict, headers: dict,
) -> tuple[int, str | None, bytes | None]:
    """One paced GET: (status, Retry-After header, body when the status is 200)."""
    await _PACER.wait()
    try:
        async with client.stream("GET", SPARQL_URL, params=params, headers=headers) as resp:
            if resp.status_code != 200:
                return resp.status_code, resp.headers.get("Retry-After"), None
            return 200, None, await _read_capped(resp, MAX_RESPONSE_BYTES)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(type(exc).__name__) from exc


async def _query(client: httpx.AsyncClient, params: dict, headers: dict) -> bytes:
    for attempt in range(2):
        status, retry_after, body = await _attempt(client, params, headers)
        if status == 429 and attempt == 0:
            delay = _retry_after_seconds(retry_after)
            if delay > RETRY_AFTER_CAP_SECONDS:
                raise SourceUnavailable(f"HTTP 429 (Retry-After {delay:.0f}s)")
            await _sleep(delay)
            continue
        if status != 200 or body is None:
            raise SourceUnavailable(f"HTTP {status}")
        return body
    raise SourceUnavailable("HTTP 429")


def parse_bindings(data: object) -> WikidataResult:
    """Read a SPARQL JSON result (`results.bindings[]`, each var a {type, value} dict).
    A row without a company, or whose label is missing or a bare Q-id, adds nothing."""
    results = data.get("results") if isinstance(data, dict) else None
    bindings = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(bindings, list):
        raise SourceUnavailable("unreadable response")
    pi_item: str | None = None
    companies: dict[str, WikidataCompany] = {}
    for row in bindings:
        if not isinstance(row, dict):
            continue
        pi_m = _ENTITY.match(str((row.get("pi") or {}).get("value", "")))
        if pi_m and pi_item is None:
            pi_item = pi_m.group(1)
        co_m = _ENTITY.match(str((row.get("company") or {}).get("value", "")))
        label = str((row.get("companyLabel") or {}).get("value", "")).strip()
        if not co_m or not label or _BARE_QID.match(label) or len(label) > 200:
            continue
        qid = co_m.group(1)
        companies.setdefault(qid, WikidataCompany(company_name=label, item=qid, url=item_url(qid)))
    return WikidataResult(pi_item=pi_item, companies=list(companies.values()))


async def founded_by_orcid(orcid: str, *, contact: str | None) -> WikidataResult:
    """Companies whose P112 is the PI's item. Raises `SourceUnavailable` on a missing
    contact or ORCID, a transport error, a non-200 answer (429 after one retry, or at
    once when its Retry-After exceeds the cap), or an oversize or unreadable body."""
    if not contact:
        raise SourceUnavailable("no contact for the User-Agent (SEC_USER_AGENT unset)")
    if not _ORCID.match(orcid or ""):
        raise SourceUnavailable("no valid ORCID")
    params = {"query": _QUERY.format(orcid=orcid), "format": "json"}
    # identity: the response cap counts decoded bytes (see sec_form_d).
    headers = {"User-Agent": user_agent(contact), "Accept": "application/sparql-results+json",
               "Accept-Encoding": "identity"}
    async with _make_client() as client:
        body = await _query(client, params, headers)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise SourceUnavailable("unreadable response") from exc
    return parse_bindings(data)
