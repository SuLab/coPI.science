"""Funding from SEC Form D (spec §7.5 Step 2, O12, O13).

Per candidate company: ONE EDGAR full-text search for the exact name in quotes,
restricted to Form D notices, then each hit's `primary_doc.xml` from the archive path
SEC documents (`/Archives/edgar/data/<CIK>/<accession without dashes>/`,
https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). A
document counts only when its issuer's normalised name equals the candidate's.

Read from live responses on 2026-10-02:

* `GET https://efts.sec.gov/LATEST/search-index?q="<name>"&forms=D` filters on
  `root_forms`, which is "D" for a D and a D/A alike (a D/A hit has `form: "D/A"`,
  `root_forms: ["D"]`; `forms=D,D/A` would keep only D/As). Hits are `hits.hits[]`
  with `_id` "<accession>:primary_doc.xml" and `_source.{adsh, ciks, file_date, form,
  file_num}`; a D and its D/A share `file_num`. The endpoint is NOT a documented SEC
  API (spec F15): if it changes, candidates get "funding lookup unavailable", never a
  wrong figure.
* `primary_doc.xml` (e.g. .../data/2021597/000202159724000002/primary_doc.xml, a D/A):
  root `edgarSubmission`; `submissionType` "D" or "D/A"; `primaryIssuer/{cik,
  entityName}`; `offeringData/typeOfFiling/newOrAmendment/{isAmendment,
  previousAccessionNumber}`; `offeringData/offeringSalesAmounts/{totalOfferingAmount,
  totalAmountSold}`, where totalOfferingAmount may be the literal "Indefinite" (SEC's
  Form D data dictionary types it ALPHANUMERIC, totalAmountSold NUMERIC);
  `relatedPersonsList/relatedPersonInfo/{relatedPersonName/{firstName, lastName},
  relatedPersonRelationshipList/relationship}`. Filers put honorifics in firstName
  ("Dr. Randall").
* SEC fair access: at most 10 requests/second (this client: 1/second) and a declared
  `User-Agent: <Company Name> <admin email>`, which is `SEC_USER_AGENT` verbatim. Every
  request goes through `_PACER`. Redirects are not followed (a redirect would be an
  unpaced extra request carrying the operator's User-Agent, possibly to another host):
  any non-200 answer, a 3xx included, makes the candidate unavailable, and so does a
  body over `MAX_RESPONSE_BYTES`, which is abandoned without being read to the end.

One issuer per candidate: filings whose issuer name matches but which come from
different CIKs are not summed. The CIK whose filings list the PI as a related person is
used when exactly one does; otherwise, with more than one CIK, no figure is recorded
(status "ambiguous", `AMBIGUOUS_ISSUER` as the note, the CIKs listed).

The floor total (O13) sums, over distinct offerings, the amount sold in the latest filing
of each offering. Filings are one offering when an amendment names the other as
`previousAccessionNumber` or the search reports the same Form D file number. A filing
whose amount sold is "Indefinite", blank or missing stays in the evidence with the
reason and is not counted (Review Focus #1); when it is the latest filing of its
offering, the offering contributes nothing, and an older filing's amount is never used
in its place.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date

import httpx

from src.services.company_sources import (
    PiName,
    SourceUnavailable,
    name_key,
    strip_honorifics,
    surname_keys,
)
from src.services.http_pacing import Pacer
from src.services.pi_companies import normalize_company_name

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}"
FILINGS_PAGE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    "&type=D&dateb=&owner=include&count=40"
)
FUNDING_UNAVAILABLE = "funding lookup unavailable"
AMBIGUOUS_ISSUER = "funding not attributed: several SEC issuers share this name"
#: Largest search answer or document read; a bigger body makes the candidate unavailable.
MAX_RESPONSE_BYTES = 2_000_000
#: Documents fetched per candidate, newest first; the rest are listed as not fetched.
MAX_DOCUMENTS = 20

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CIK = re.compile(r"^\d{1,10}$")
_FILENAME = re.compile(r"^[A-Za-z0-9_.\-]+\.xml$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _interval() -> float:
    """One request per second (spec O12); a function so tests can zero it."""
    return 1.0


_PACER = Pacer(lambda: _interval())


@dataclass
class FormDFiling:
    accession: str
    filing_date: str
    form: str
    cik: str
    file_num: str | None
    issuer_name: str
    is_amendment: bool
    previous_accession: str | None
    total_offering_amount: int | None
    total_offering_amount_raw: str | None
    total_amount_sold: int | None
    total_amount_sold_raw: str | None
    amount_note: str | None
    pi_listed: bool
    pi_relationships: list[str]
    url: str
    counted: bool = False


@dataclass
class FundingResult:
    status: str  # "ok" | "no_amount" | "no_filings" | "ambiguous" | "unavailable"
    funding_usd: int | None = None
    funding_as_of: date | None = None
    funding_source_url: str | None = None
    filings: list[FormDFiling] = field(default_factory=list)
    reason: str | None = None
    not_fetched: int = 0
    #: The CIK whose filings were counted (10 digits); None unless one issuer was chosen.
    issuer_cik: str | None = None
    #: Every CIK (10 digits, sorted) with a filing under the candidate's name.
    ciks: list[str] = field(default_factory=list)
    note: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> FundingResult:
        return cls(status="unavailable", reason=reason)

    def evidence(self) -> dict:
        out: dict = {
            "status": self.status,
            "funding_usd": self.funding_usd,
            "funding_as_of": self.funding_as_of.isoformat() if self.funding_as_of else None,
            "filings_page": self.funding_source_url,
            "filings": [asdict(f) for f in self.filings],
            "not_fetched": self.not_fetched,
            "issuer_cik": self.issuer_cik,
            "ciks": list(self.ciks),
        }
        if self.note:
            out["note"] = self.note
        if self.status == "unavailable":
            out["note"] = FUNDING_UNAVAILABLE
            out["reason"] = self.reason
        return out


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam for tests."""
    return httpx.AsyncClient(timeout=30, follow_redirects=False)


def filings_page_url(cik: str) -> str:
    return FILINGS_PAGE_URL.format(cik=cik.zfill(10))


def parse_amount(raw: str | None, label: str) -> tuple[int | None, str | None]:
    """A Form D dollar amount, or None with the reason: "Indefinite", blank, missing or
    unreadable amounts are never read as 0 (Review Focus #1)."""
    if raw is None:
        return None, f"{label} missing"
    text = raw.strip()
    if not text:
        return None, f"{label} blank"
    if text.casefold() == "indefinite":
        return None, f"{label} 'Indefinite'"
    cleaned = text.replace(",", "").replace("$", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return int(cleaned.split(".")[0]), None
    return None, f"{label} unreadable ({text[:40]!r})"


def _strip_namespaces(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None if el is None else ""
    return el.text.strip()


def _is_pi(first: str, last: str, pi: PiName) -> bool:
    """Exact related-person match: the folded given name equals the PI's (after any
    honorific), and the folded surname is one of the PI's surname forms."""
    given = strip_honorifics(first).split()
    if not given or name_key(given[0]) != name_key(pi.first):
        return False
    return bool(surname_keys(last) & pi.surname_keys)


def parse_form_d(
    xml_text: str, *, accession: str, filing_date: str, form: str, cik: str,
    file_num: str | None, url: str, pi: PiName | None,
) -> FormDFiling:
    """One `primary_doc.xml`. Raises `SourceUnavailable` when it is not a readable Form D."""
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise SourceUnavailable("unreadable document (DTD present)")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceUnavailable("unreadable document") from exc
    _strip_namespaces(root)
    if root.tag != "edgarSubmission":
        raise SourceUnavailable("unreadable document (not a Form D)")
    submission = _text(root, "submissionType") or form
    issuer = _text(root, "primaryIssuer/entityName") or ""
    is_amendment = (_text(root, "offeringData/typeOfFiling/newOrAmendment/isAmendment") or "").casefold() == "true"
    is_amendment = is_amendment or submission == "D/A"
    previous = _text(root, "offeringData/typeOfFiling/newOrAmendment/previousAccessionNumber") or None
    offered_raw = _text(root, "offeringData/offeringSalesAmounts/totalOfferingAmount")
    sold_raw = _text(root, "offeringData/offeringSalesAmounts/totalAmountSold")
    offered, offered_note = parse_amount(offered_raw, "totalOfferingAmount")
    sold, sold_note = parse_amount(sold_raw, "totalAmountSold")
    relationships: list[str] = []
    listed = False
    if pi is not None:
        for person in root.findall("relatedPersonsList/relatedPersonInfo"):
            first = _text(person, "relatedPersonName/firstName") or ""
            last = _text(person, "relatedPersonName/lastName") or ""
            if _is_pi(first, last, pi):
                listed = True
                for rel in person.findall("relatedPersonRelationshipList/relationship"):
                    if rel.text and rel.text.strip() and rel.text.strip() not in relationships:
                        relationships.append(rel.text.strip())
    notes = [n for n in (sold_note and f"{sold_note}; not counted", offered_note) if n]
    return FormDFiling(
        accession=accession, filing_date=filing_date, form=submission, cik=cik,
        file_num=file_num, issuer_name=issuer, is_amendment=is_amendment,
        previous_accession=previous, total_offering_amount=offered,
        total_offering_amount_raw=offered_raw, total_amount_sold=sold,
        total_amount_sold_raw=sold_raw, amount_note="; ".join(notes) or None,
        pi_listed=listed, pi_relationships=relationships, url=url,
    )


_OFFERING_NOT_COUNTED = "latest filing of its offering, so the offering is not counted"


def _mark_offering_not_counted(latest: FormDFiling) -> None:
    note = latest.amount_note or ""
    if _OFFERING_NOT_COUNTED not in note:
        latest.amount_note = f"{note}; {_OFFERING_NOT_COUNTED}" if note else _OFFERING_NOT_COUNTED


def floor_total(filings: list[FormDFiling]) -> tuple[int | None, date | None]:
    """O13: per distinct offering, the amount sold in its latest filing; summed. An
    offering whose latest filing states no amount sold contributes nothing (an older
    filing's amount is not used) and that filing's `amount_note` says so. Marks the
    counted filings. Returns (total, newest counted filing date); a total of 0 is
    returned as None (nothing to show as "raised")."""
    parent = {f.accession: f.accession for f in filings}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_file_num: dict[str, str] = {}
    for f in filings:
        if f.previous_accession and f.previous_accession in parent:
            union(f.accession, f.previous_accession)
        if f.file_num:
            if f.file_num in by_file_num:
                union(f.accession, by_file_num[f.file_num])
            else:
                by_file_num[f.file_num] = f.accession
    groups: dict[str, list[FormDFiling]] = {}
    for f in filings:
        f.counted = False
        groups.setdefault(find(f.accession), []).append(f)
    total, newest = 0, None
    for members in groups.values():
        members.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
        chosen = members[0]
        if chosen.total_amount_sold is None:
            if len(members) > 1:
                _mark_offering_not_counted(chosen)
            continue
        chosen.counted = True
        total += chosen.total_amount_sold or 0
        filed = date.fromisoformat(chosen.filing_date)
        newest = filed if newest is None or filed > newest else newest
    if newest is None:
        return None, None
    return (total or None), newest


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


async def _get(
    client: httpx.AsyncClient, url: str, *, headers: dict, params: dict | None = None,
) -> tuple[bytes, str]:
    """One paced GET: (body, text encoding). Any non-200 answer (redirects are not
    followed), a transport error or an oversize body raises `SourceUnavailable`."""
    await _PACER.wait()
    try:
        async with client.stream("GET", url, params=params, headers=headers) as resp:
            if resp.status_code != 200:
                raise SourceUnavailable(f"HTTP {resp.status_code}")
            body = await _read_capped(resp, MAX_RESPONSE_BYTES)
            return body, resp.encoding or "utf-8"
    except httpx.HTTPError as exc:
        raise SourceUnavailable(type(exc).__name__) from exc


def _hits(data: object) -> list[dict]:
    """Form D / D/A hits with a usable accession, CIK and filing date, newest first."""
    outer = data.get("hits") if isinstance(data, dict) else None
    raw = outer.get("hits") if isinstance(outer, dict) else None
    if not isinstance(raw, list):
        raise SourceUnavailable("unreadable search response")
    out: list[dict] = []
    seen: set[str] = set()
    for hit in raw:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(src, dict) or src.get("form") not in ("D", "D/A"):
            continue
        accession = str(src.get("adsh") or "")
        ciks = src.get("ciks") or []
        cik = str(ciks[0]) if isinstance(ciks, list) and ciks else ""
        filed = str(src.get("file_date") or "")
        if not (_ACCESSION.match(accession) and _CIK.match(cik) and _ISO_DATE.match(filed)):
            continue
        if accession in seen:
            continue
        seen.add(accession)
        filename = str(hit.get("_id") or "").partition(":")[2] or "primary_doc.xml"
        if not _FILENAME.match(filename):
            filename = "primary_doc.xml"
        file_nums = src.get("file_num") or []
        out.append({
            "accession": accession, "cik": cik, "filing_date": filed, "form": src["form"],
            "file_num": str(file_nums[0]) if isinstance(file_nums, list) and file_nums else None,
            "url": ARCHIVE_DOC_URL.format(cik=int(cik), accession=accession.replace("-", ""), filename=filename),
        })
    out.sort(key=lambda h: (h["filing_date"], h["accession"]), reverse=True)
    return out


def choose_issuer(filings: list[FormDFiling]) -> tuple[str | None, list[str]]:
    """(the CIK to count, every CIK seen), CIKs zero-padded to 10 digits. One CIK is
    used as is; among several, the one whose filings list the PI, when exactly one
    does; otherwise None (several issuers share the name: no figure is attributed)."""
    by_cik: dict[str, list[FormDFiling]] = {}
    for f in filings:
        by_cik.setdefault(f.cik.zfill(10), []).append(f)
    ciks = sorted(by_cik)
    if len(ciks) == 1:
        return ciks[0], ciks
    listed = [c for c in ciks if any(f.pi_listed for f in by_cik[c])]
    return (listed[0] if len(listed) == 1 else None), ciks


async def _fetch_filings(
    client: httpx.AsyncClient, company_name: str, normalized_name: str, pi: PiName | None,
    headers: dict,
) -> tuple[list[FormDFiling], int]:
    """(filings whose issuer name matches, hits not fetched). Raises `SourceUnavailable`."""
    body, _encoding = await _get(
        client, EFTS_URL, headers=headers, params={"q": f'"{company_name}"', "forms": "D"},
    )
    try:
        hits = _hits(json.loads(body))
    except ValueError as exc:
        raise SourceUnavailable("unreadable search response") from exc
    filings: list[FormDFiling] = []
    for hit in hits[:MAX_DOCUMENTS]:
        doc, encoding = await _get(client, hit["url"], headers=headers)
        filing = parse_form_d(
            doc.decode(encoding, errors="replace"), accession=hit["accession"],
            filing_date=hit["filing_date"], form=hit["form"], cik=hit["cik"],
            file_num=hit["file_num"], url=hit["url"], pi=pi,
        )
        if normalize_company_name(filing.issuer_name) == normalized_name:
            filings.append(filing)
    return filings, max(0, len(hits) - MAX_DOCUMENTS)


async def lookup_funding(
    company_name: str, normalized_name: str, pi: PiName | None, *, user_agent: str,
) -> FundingResult:
    """Form D funding for one candidate. Never raises for an upstream problem: an unset
    `SEC_USER_AGENT`, a transport or status failure (a redirect included), an oversize
    body or an unreadable document makes the whole candidate `unavailable` ("funding
    lookup unavailable"). Several issuers under the name, none singled out by the PI,
    give `ambiguous` with no figure."""
    agent = (user_agent or "").strip()
    if not agent:
        return FundingResult.unavailable("SEC_USER_AGENT unset")
    try:
        async with _make_client() as client:
            filings, not_fetched = await _fetch_filings(
                client, company_name, normalized_name, pi, {"User-Agent": agent})
    except SourceUnavailable as exc:
        return FundingResult.unavailable(str(exc))
    if not filings:
        return FundingResult(status="no_filings", not_fetched=not_fetched)
    issuer, ciks = choose_issuer(filings)
    if issuer is None:
        return FundingResult(
            status="ambiguous", filings=filings, not_fetched=not_fetched, ciks=ciks,
            note=AMBIGUOUS_ISSUER,
        )
    total, as_of = floor_total([f for f in filings if f.cik.zfill(10) == issuer])
    return FundingResult(
        status="ok" if total is not None else "no_amount",
        funding_usd=total, funding_as_of=as_of if total is not None else None,
        funding_source_url=filings_page_url(issuer), filings=filings, not_fetched=not_fetched,
        issuer_cik=issuer, ciks=ciks,
    )
