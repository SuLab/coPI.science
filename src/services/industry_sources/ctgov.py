import httpx

from src.services.industry_sources import EvidenceItem, Paged
from src.services.industry_sources.companies import classify_company
from src.services.industry_sources.registry import SourceUnavailable
from src.services.person_names import parse_person_name
from src.services.query_escaping import phrase_term

BASE = "https://clinicaltrials.gov/api/v2/studies"
# The v2 legacy flat field names (NCTId, BriefTitle, ...) do NOT return the
# nested protocolSection.* shape evidence_from_study parses — requesting the
# module names directly does (verified live 2026-09-11).
_FIELDS = ("protocolSection.identificationModule,protocolSection.sponsorCollaboratorsModule,"
           "protocolSection.statusModule,protocolSection.conditionsModule")
_JHU_SPONSOR_MARKERS = ("johns hopkins", "sidney kimmel")


#: Studies per request, and the most requests one PI's search makes (spec 2026-10-05
#: §6.2, D12).
PAGE_SIZE = 100
MAX_PAGES = 10


def official_term(full_name: str | None) -> str | None:
    """The ClinicalTrials.gov term for studies with the PI as an overall official and an
    industry collaborator, the name parsed and escaped for the Essie phrase (P29). None
    for an ORCID-iD name or one that leaves nothing to search on."""
    parsed = parse_person_name(full_name)
    name = "" if parsed.is_orcid_id else phrase_term(parsed.full)
    if not name:
        return None
    return f'AREA[OverallOfficialName]"{name}" AND AREA[CollaboratorClass]INDUSTRY'


async def fetch_jhu_industry_trials(pi_name: str | None) -> Paged:
    """Every matching study, PAGE_SIZE a request, following `nextPageToken` (API v2; checked
    live 2026-10-06: the token comes back with `fields=` restricted, and a search with no
    more results returns none); `truncated` after MAX_PAGES pages with a token left. Raises
    SourceUnavailable without a usable name."""
    term = official_term(pi_name)
    if term is None:
        raise SourceUnavailable("ctgov: no usable official name", reason="no_usable_name")
    studies: list[dict] = []
    token: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(MAX_PAGES):
            params: dict[str, str | int] = {"query.term": term, "fields": _FIELDS,
                                            "pageSize": PAGE_SIZE}
            if token:
                params["pageToken"] = token
            resp = await client.get(BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
            studies.extend(data.get("studies") or [])
            token = data.get("nextPageToken")
            if not token:
                return Paged(studies)
    return Paged(studies, truncated=True)


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
