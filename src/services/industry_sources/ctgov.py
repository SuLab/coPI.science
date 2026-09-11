import httpx

from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

BASE = "https://clinicaltrials.gov/api/v2/studies"
# The v2 legacy flat field names (NCTId, BriefTitle, ...) do NOT return the
# nested protocolSection.* shape evidence_from_study parses — requesting the
# module names directly does (verified live 2026-09-11).
_FIELDS = ("protocolSection.identificationModule,protocolSection.sponsorCollaboratorsModule,"
           "protocolSection.statusModule,protocolSection.conditionsModule")
_JHU_SPONSOR_MARKERS = ("johns hopkins", "sidney kimmel")


async def fetch_jhu_industry_trials(pi_name: str) -> list[dict]:
    term = f'AREA[OverallOfficialName]"{pi_name}" AND AREA[CollaboratorClass]INDUSTRY'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(BASE, params={"query.term": term, "fields": _FIELDS, "pageSize": 100})
        resp.raise_for_status()
        return resp.json().get("studies") or []


def evidence_from_study(study: dict, tenure_start: int | None, pi_conditions: set[str]) -> list[EvidenceItem]:
    ps = study.get("protocolSection") or {}
    lead = (ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}
    if lead.get("class") == "INDUSTRY" or not any(m in (lead.get("name") or "").lower() for m in _JHU_SPONSOR_MARKERS):
        return []
    start = ((ps.get("statusModule") or {}).get("startDateStruct") or {}).get("date") or ""
    year = int(start[:4]) if start[:4].isdigit() else None
    if tenure_start is None or year is None or year < tenure_start:
        return []
    conds = {c.lower() for c in (ps.get("conditionsModule") or {}).get("conditions") or []}
    if not any(pc.lower() in c or c in pc.lower() for c in conds for pc in pi_conditions):
        return []
    nct = (ps.get("identificationModule") or {}).get("nctId")
    items = []
    for c in (ps.get("sponsorCollaboratorsModule") or {}).get("collaborators") or []:
        if c.get("class") != "INDUSTRY":
            continue
        name = c.get("name")
        if not name or not nct:
            continue
        items.append(EvidenceItem("ctgov", "trial_industry_collab", f"{nct}:{name.lower()}", name, None,
                                  classify_company(name, "company"), year, "overall_official", True,
                                  {"nct_id": nct, "title": (ps.get("identificationModule") or {}).get("briefTitle"), "lead_sponsor": lead.get("name")}))
    return items
