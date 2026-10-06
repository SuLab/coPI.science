import re

from src.config import get_settings
from src.services.industry_sources import EvidenceItem, Paged
from src.services.industry_sources.companies import classify_company
from src.services.industry_sources.registry import SourceUnavailable
from src.services.odp import ODP_PACER, ODP_SEARCH_URL, odp_client, odp_headers
from src.services.person_names import parse_person_name
from src.services.query_escaping import phrase_term

SEARCH_URL = ODP_SEARCH_URL
_FIELDS = ["applicationNumberText", "applicationMetaData.inventionTitle", "applicationMetaData.filingDate",
           "applicationMetaData.firstInventorName", "applicationMetaData.applicantBag", "assignmentBag"]
# [a-z0-9]+ naturally splits on hyphens ("beta-lactam" -> "beta", "lactam"),
# so no explicit hyphen handling is needed. A 4-char floor excluded common
# short acronyms (TB, HIV, RNA); the stopword set instead screens out
# boilerplate patent-title words that would otherwise "match" on their own.
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"for", "and", "the", "of", "method", "methods", "treatment", "treating",
              "composition", "compositions", "use", "uses", "thereof", "with", "using"}
_ACADEMIC = re.compile(r"\b(university|institute|college|hospital|foundation)\b", re.I)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) >= 2 and t not in _STOPWORDS}


def _is_jhu(name: str) -> bool:
    return "johns hopkins" in (name or "").lower()


def _is_industry_company(name: str) -> bool:
    """True only for a non-JHU, non-academic name that classifies as an
    actual industry company — never ``other`` (which would admit any
    university, hospital or foundation not literally named "Johns Hopkins")
    or ``cro_vendor``/``unknown``."""
    if not name or _is_jhu(name) or _ACADEMIC.search(name):
        return False
    return classify_company(name, "company") in ("pharma_biotech", "device_dx")


#: Applications per ODP request, and the most requests one PI's search makes (spec
#: 2026-10-05 §6.2, D12). A PI past 1,000 JHU applications is recorded as truncated.
ODP_PAGE_LIMIT = 100
MAX_PAGES = 10


def inventor_query(full_name: str | None) -> str | None:
    """The ODP query for applications whose first inventor is the PI and whose applicant
    is Johns Hopkins, the name parsed (`person_names.parse_person_name`: honorifics, comma
    degrees and suffixes dropped) and escaped for the phrase (`query_escaping.phrase_term`,
    P29). None for an ORCID-iD name or one that leaves nothing to search on."""
    parsed = parse_person_name(full_name)
    name = "" if parsed.is_orcid_id else phrase_term(parsed.full)
    if not name:
        return None
    return (f'applicationMetaData.firstInventorName:"{name}" AND '
            'applicationMetaData.applicantBag.applicantNameText:"Johns Hopkins"')


async def fetch_jhu_applications(inventor_full_name: str | None) -> Paged:
    """Every JHU application whose first inventor is the PI, ODP_PAGE_LIMIT a request by
    `offset` (newest filing first, deduplicated by application number), until a short
    page, the reported `count`, or a 404 (ODP's answer for "no matches", on any page);
    `truncated` after MAX_PAGES full pages with more reported. Raises SourceUnavailable
    without a key or a usable name: an empty answer would read as "no patents" and the
    job would delete the PI's stored patent rows. Every other error status raises, so the
    source is unavailable for the run."""
    key = get_settings().uspto_api_key
    if not key:
        raise SourceUnavailable("uspto: USPTO_API_KEY is not set", reason="no_api_key")
    q = inventor_query(inventor_full_name)
    if q is None:
        raise SourceUnavailable("uspto: no usable inventor name", reason="no_usable_name")
    items: dict[str, dict] = {}
    async with odp_client(timeout=30, follow_redirects=False) as client:
        for page in range(MAX_PAGES):
            await ODP_PACER.wait()
            resp = await client.post(
                SEARCH_URL, headers=odp_headers(key),
                json={"q": q,
                      "pagination": {"offset": page * ODP_PAGE_LIMIT, "limit": ODP_PAGE_LIMIT},
                      "sort": [{"field": "applicationMetaData.filingDate", "order": "desc"}],
                      "fields": _FIELDS},
            )
            if resp.status_code == 404:
                return Paged(list(items.values()))
            resp.raise_for_status()
            data = resp.json()
            bag = data.get("patentFileWrapperDataBag") or []
            for app in bag:
                items.setdefault(app.get("applicationNumberText") or f"#{len(items)}", app)
            total = data.get("count")
            reached = isinstance(total, int) and (page + 1) * ODP_PAGE_LIMIT >= total
            if len(bag) < ODP_PAGE_LIMIT or reached:
                return Paged(list(items.values()))
    return Paged(list(items.values()), truncated=True)


def evidence_from_application(app: dict, tenure_start: int | None, pi_keywords: set[str]) -> list[EvidenceItem]:
    meta = app.get("applicationMetaData") or {}
    applicants = [a.get("applicantNameText", "") for a in meta.get("applicantBag") or []]
    if not any(_is_jhu(a) for a in applicants):
        return []
    filing = meta.get("filingDate") or ""
    year = int(filing[:4]) if filing[:4].isdigit() else None
    if tenure_start is None or year is None or year < tenure_start:
        return []
    title = meta.get("inventionTitle") or ""
    kw = {k.lower() for k in pi_keywords}
    if not (_tokens(title) & {t for k in kw for t in _tokens(k)}):
        return []
    appno = app.get("applicationNumberText") or title[:40]
    base = {"title": title, "filing_date": filing, "applicants": applicants}
    items = [EvidenceItem("uspto", "patent_filed", appno, None, None, "unknown", year, "inventor", True, dict(base))]
    companies = [a for a in applicants if _is_industry_company(a)]
    for bag in app.get("assignmentBag") or []:
        companies += [s.get("assigneeNameText", "") for s in bag.get("assigneeBag") or [] if _is_industry_company(s.get("assigneeNameText", ""))]
    for c in dict.fromkeys(c for c in companies if c):
        items.append(EvidenceItem("uspto", "patent_assigned", f"{appno}:{c.lower()}", c, None, classify_company(c, "company"), year, "inventor", True, dict(base)))
    return items
