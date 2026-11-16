"""The JHTV technologies source: the Johns Hopkins Technology Ventures listing feed (spec
2026-10-30 §6.1), read as RSS only (no page scraping: the terms of use are unverified, and
the source starts disabled until a person has read them, §15).

The whole feed is read each time; the §5.3 window is applied at S1.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from src.landscaping.sources import DocumentRecord, SliceResult
from src.services.http_pacing import TRANSIENT_STATUSES, with_retries

RSS_URL = "https://jhu.technologypublisher.com/rss.aspx"

_TAG = re.compile(r"<[^>]+>")
# RSS 2.0 `author` is "email (Name)"; the name is what the lab key needs.
_EMAIL_AUTHOR = re.compile(r"^\s*\S+@\S+\s*\((?P<name>[^)]*)\)\s*$")


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=30, follow_redirects=True)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _child(item: ET.Element, name: str) -> ET.Element | None:
    """The first child whose local name (any namespace) is `name`, case-insensitive."""
    return next((c for c in item if _local(c.tag) == name), None)


def _case_id(item: ET.Element, link: str, guid: str) -> str | None:
    """The item's caseId: a `caseId` element in any namespace, else a ``caseid`` query
    parameter of its link or guid."""
    explicit = _text(_child(item, "caseid"))
    if explicit:
        return explicit
    for url in (link, guid):
        for key, values in parse_qs(urlsplit(url).query).items():
            if key.lower() == "caseid" and values and values[0].strip():
                return values[0].strip()
    return None


def _pub_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        return None


def _author(item: ET.Element) -> str:
    raw = _text(_child(item, "author")) or _text(_child(item, "creator"))
    match = _EMAIL_AUTHOR.match(raw)
    return " ".join((match.group("name") if match else raw).split())


def parse_rss(xml_text: str) -> list[dict]:
    """Each RSS `item` as a dict: title, link, guid, case_id, pub_date (the raw RFC 822
    text), author, description (plain text) and categories. RAISES `ET.ParseError` on a
    malformed feed."""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        link = _text(_child(item, "link"))
        guid = _text(_child(item, "guid"))
        description = html.unescape(_TAG.sub(" ", _text(_child(item, "description"))))
        items.append({
            "title": _text(_child(item, "title")),
            "link": link,
            "guid": guid,
            "case_id": _case_id(item, link, guid),
            "pub_date": _text(_child(item, "pubdate")),
            "author": _author(item),
            "description": " ".join(description.split()),
            "categories": [_text(c) for c in item if _local(c.tag) == "category" and _text(c)],
        })
    return items


def to_document(item: dict) -> DocumentRecord:
    """One parsed feed item (`parse_rss`) as a JHTV listing. Every listing qualifies
    (`role_basis` ``"jhtv"``, §5.2); `ids["jhtv_case"]` is the caseId, else the guid, else
    the link; `senior_names` holds the item's author (the listed inventor or contact)."""
    case = item.get("case_id") or item.get("guid") or item.get("link")
    summary = item.get("description") or None
    author = item.get("author") or ""
    return DocumentRecord(
        kind="jhtv",
        ids={"jhtv_case": case} if case else {},
        title=item.get("title") or "",
        summary=summary,
        published=_pub_date(item.get("pub_date") or ""),
        doc_type="technology",
        language=None,
        license=None,
        authorships=None,
        role_basis="jhtv",
        summary_source="jhtv" if summary else None,
        senior_names=[{"name": author, "orcid": None, "openalex_author_id": None,
                       "nih_pi_profile_id": None}] if author else [],
        extra={"guid": item.get("guid"), "link": item.get("link"),
               "categories": list(item.get("categories") or [])},
    )


async def fetch_rss() -> SliceResult:
    """The whole feed, one record per item; `expected` and `received` are both the feed's
    item count (RSS reports no separate total). RAISES on an HTTP error after retries or a
    malformed feed."""
    async with _make_client() as client:
        resp = await with_retries(
            lambda: client.get(RSS_URL),
            attempts=3, backoff=lambda a: 2.0 * (a + 1), retry_statuses=TRANSIENT_STATUSES,
        )
    resp.raise_for_status()
    items = parse_rss(resp.text)
    return SliceResult(rows=[to_document(i) for i in items], expected=len(items),
                       received=len(items))
