"""OpenAlex-derived industry evidence: company co-authors and company funders."""
import logging
import re

import httpx

from src.config import get_settings
from src.services.industry_sources import JHU_OPENALEX_IDS, EvidenceItem
from src.services.industry_sources.companies import classify_company
from src.services.jhu_rules import is_hopkins_affiliation

logger = logging.getLogger(__name__)
OA = "https://api.openalex.org"
_SELECT = "id,ids,title,publication_year,authorships,funders,primary_topic"


def _oaid(url: str | None) -> str:
    return (url or "").rsplit("/", 1)[-1]


_ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def _pi_authorship(work: dict, pi_orcid: str) -> dict | None:
    if not pi_orcid:
        return None
    tail = pi_orcid.rsplit("/", 1)[-1]
    if not _ORCID_RE.search(tail):
        return None
    for a in work.get("authorships") or []:
        if (a.get("author") or {}).get("orcid", "") and a["author"]["orcid"].endswith(tail):
            return a
    return None


def _pi_at_jhu(auth: dict) -> bool:
    if any(_oaid(i.get("id")) in JHU_OPENALEX_IDS for i in auth.get("institutions") or []):
        return True
    return any(is_hopkins_affiliation(s) for s in auth.get("raw_affiliation_strings") or [])


def _role(auth: dict) -> str:
    if auth.get("is_corresponding"):
        return "corresponding"
    return auth.get("author_position") or "middle"


def evidence_from_work(work: dict, pi_orcid: str, tenure_start: int | None, *, company_funder_ids: set[str]) -> list[EvidenceItem]:
    year = work.get("publication_year")
    if tenure_start is None or not year or year < tenure_start:
        return []
    me = _pi_authorship(work, pi_orcid)
    if me is None or not _pi_at_jhu(me):
        return []
    wid = _oaid(work.get("id"))
    n_authors = len(work.get("authorships") or [])
    base = {"work_id": wid, "pmid": _oaid((work.get("ids") or {}).get("pmid")), "title": work.get("title"), "author_count": n_authors}
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for a in work.get("authorships") or []:
        if a is me:
            continue
        for inst in a.get("institutions") or []:
            if inst.get("type") != "company":
                continue
            cid = _oaid(inst.get("id"))
            if cid in seen:
                continue
            seen.add(cid)
            items.append(EvidenceItem("openalex", "coauthor_company", f"{wid}:{cid}", inst.get("display_name"), cid,
                                      classify_company(inst.get("display_name", ""), "company"), year, _role(me), True, dict(base)))
    for f in work.get("funders") or []:
        fid = _oaid(f.get("id"))
        if fid in company_funder_ids:
            items.append(EvidenceItem("openalex", "company_funder", f"{wid}:{fid}", f.get("display_name"), fid,
                                      classify_company(f.get("display_name", ""), "company"), year, _role(me), True, dict(base)))
    return items


async def fetch_works_for_pmids(pmids: list[str]) -> list[dict]:
    out: list[dict] = []
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(pmids), 50):
            chunk = "|".join(pmids[i:i + 50])
            params = {"filter": f"pmid:{chunk}", "select": _SELECT, "per-page": 50}
            if contact:
                params["mailto"] = contact
            resp = await client.get(f"{OA}/works", params=params)
            resp.raise_for_status()
            out.extend(resp.json().get("results") or [])
    return out


async def company_funder_ids(funder_ids: set[str]) -> set[str]:
    """Funder ids whose entity also has an ``institution`` role of type company (e.g. GSK).

    One funders request and one institutions request per chunk of 50 funders (DP-12);
    the answer is the same set the per-funder lookup returned."""
    if not funder_ids:
        return set()
    hits: set[str] = set()
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    mailto = {"mailto": contact} if contact else {}
    ordered = sorted(funder_ids)
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(ordered), 50):
            chunk = "|".join(ordered[i:i + 50])
            resp = await client.get(f"{OA}/funders", params={
                "filter": f"ids.openalex:{chunk}", "select": "id,display_name,roles", "per-page": 50, **mailto,
            })
            resp.raise_for_status()
            inst_by_funder: dict[str, list[str]] = {}
            for f in resp.json().get("results") or []:
                inst_ids = [_oaid(r.get("id")) for r in f.get("roles") or [] if r.get("role") == "institution"]
                if inst_ids:
                    inst_by_funder[_oaid(f.get("id"))] = inst_ids
            all_insts = sorted({i for ids in inst_by_funder.values() for i in ids})
            if not all_insts:
                continue
            companies: set[str] = set()
            for j in range(0, len(all_insts), 50):
                r2 = await client.get(f"{OA}/institutions", params={
                    "filter": f"ids.openalex:{'|'.join(all_insts[j:j + 50])}",
                    "select": "id,type", "per-page": 50, **mailto,
                })
                r2.raise_for_status()
                companies |= {_oaid(i.get("id")) for i in r2.json().get("results") or []
                              if i.get("type") == "company"}
            hits |= {fid for fid, ids in inst_by_funder.items() if companies & set(ids)}
    return hits
