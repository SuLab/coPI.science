import re

import httpx

from src.config import get_settings
from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
_FIELDS = ["applicationNumberText", "applicationMetaData.inventionTitle", "applicationMetaData.filingDate",
           "applicationMetaData.firstInventorName", "applicationMetaData.applicantBag", "assignmentBag"]
_TOKEN = re.compile(r"[a-z][a-z0-9\-]{3,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def _is_jhu(name: str) -> bool:
    return "johns hopkins" in (name or "").lower()


async def fetch_jhu_applications(inventor_full_name: str) -> list[dict]:
    key = get_settings().uspto_api_key
    if not key:
        return []
    q = f'applicationMetaData.firstInventorName:"{inventor_full_name}" AND applicationMetaData.applicantBag.applicantNameText:"Johns Hopkins"'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(SEARCH_URL, headers={"X-API-KEY": key}, json={"q": q, "pagination": {"offset": 0, "limit": 100}, "fields": _FIELDS})
        resp.raise_for_status()
        return resp.json().get("patentFileWrapperDataBag") or []


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
    companies = [a for a in applicants if not _is_jhu(a) and classify_company(a, "company") != "other"]
    for bag in app.get("assignmentBag") or []:
        companies += [s.get("assigneeNameText", "") for s in bag.get("assigneeBag") or [] if not _is_jhu(s.get("assigneeNameText", ""))]
    for c in dict.fromkeys(c for c in companies if c):
        cls = classify_company(c, "company")
        if cls in ("pharma_biotech", "device_dx", "other"):
            items.append(EvidenceItem("uspto", "patent_assigned", f"{appno}:{c.lower()}", c, None, cls, year, "inventor", True, dict(base)))
    return items
