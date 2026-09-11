"""Turn RePORTER rows into per-PI grant records: identity, tenure, collapse.

Identity rule (adversarial analysis A1–A5): a RePORTER ``profile_id`` is the
PI iff one of its projects links (RePORTER publications endpoint) to a PMID in
the PI's own ORCID/PubMed-anchored corpus; failing that, iff it is the ONLY
Hopkins candidate with an exact first-name match. Two unlinked candidates →
nobody, never a guess.

Tenure rule (B6–B8): a fiscal-year row counts iff org == JHU exact AND
fiscal_year >= tenure_start. With no tenure start the year half is skipped and
the mode is reported as ``org_only`` so the UI can show the weaker guarantee.
"""
from dataclasses import dataclass
from datetime import UTC, datetime

from src.services.nih_reporter import JHU_ORG_EXACT

LLM_ACTIVITY_CODES = frozenset({
    "R01", "R21", "R33", "R35", "R37", "R00", "R56", "R03", "RF1", "DP1", "DP2",
    "U01", "U19", "UG3", "UH3", "P01", "K08", "K23", "K99", "R41", "R42", "R43", "R44",
})


@dataclass
class GrantRecord:
    core_project_num: str
    reporter_profile_id: int | None
    title: str
    phr_text: str | None
    terms: str | None
    activity_code: str | None
    agency_ic: str | None
    funding_mechanism: str | None
    org_name: str
    first_fy: int | None
    last_fy: int | None
    project_start: datetime | None
    project_end: datetime | None
    total_award_in_tenure: int | None
    is_contact_pi: bool | None
    is_subproject: bool


def _org(row: dict) -> str:
    return ((row.get("organization") or {}).get("org_name") or "").strip().upper()


def _pis(row: dict) -> list[dict]:
    return row.get("principal_investigators") or []


def resolve_profile_ids(rows: list[dict], corpus_pmids: set[str], links: dict[str, set[str]],
                        first_name: str) -> tuple[set[int], dict]:
    cores_by_pid: dict[int, set[str]] = {}
    first_by_pid: dict[int, set[str]] = {}
    for r in rows:
        if _org(r) != JHU_ORG_EXACT:
            continue
        for p in _pis(r):
            pid = p.get("profile_id")
            if pid is None:
                continue
            cores_by_pid.setdefault(pid, set()).add(r["core_project_num"])
            first_by_pid.setdefault(pid, set()).add((p.get("first_name") or "").strip().lower())
    pmid_linked = [pid for pid, cores in cores_by_pid.items()
                   if any(links.get(c, set()) & corpus_pmids for c in cores)]
    accepted = set(pmid_linked)
    unique_name: list[int] = []
    if not accepted and len(cores_by_pid) == 1:
        (pid, _), = cores_by_pid.items()
        if first_name.strip().lower() in first_by_pid[pid]:
            accepted.add(pid)
            unique_name.append(pid)
    rejected = sorted(set(cores_by_pid) - accepted)
    return accepted, {"pmid_linked": sorted(pmid_linked), "unique_name": unique_name, "rejected": rejected}


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def filter_and_collapse(rows: list[dict], profile_ids: set[int], tenure_start: int | None
                        ) -> tuple[list[GrantRecord], str]:
    mode = "org_and_year" if tenure_start is not None else "org_only"
    kept: dict[str, list[dict]] = {}
    seen_project_nums: set[str] = set()
    for r in rows:
        if _org(r) != JHU_ORG_EXACT:
            continue
        if tenure_start is not None and (r.get("fiscal_year") or 0) < tenure_start:
            continue
        if not any(p.get("profile_id") in profile_ids for p in _pis(r)):
            continue
        if r.get("project_num") in seen_project_nums:
            continue
        seen_project_nums.add(r.get("project_num"))
        kept.setdefault(r["core_project_num"], []).append(r)
    records: list[GrantRecord] = []
    for core, rs in kept.items():
        rs.sort(key=lambda x: x.get("fiscal_year") or 0)
        latest = rs[-1]
        me = next((p for p in _pis(latest) if p.get("profile_id") in profile_ids), {})
        records.append(GrantRecord(
            core_project_num=core,
            reporter_profile_id=me.get("profile_id"),
            title=latest.get("project_title") or core,
            phr_text=latest.get("phr_text"),
            terms=latest.get("terms"),
            activity_code=latest.get("activity_code"),
            agency_ic=(latest.get("agency_ic_admin") or {}).get("code"),
            funding_mechanism=latest.get("funding_mechanism"),
            org_name=JHU_ORG_EXACT,
            first_fy=rs[0].get("fiscal_year"),
            last_fy=latest.get("fiscal_year"),
            project_start=_dt(rs[0].get("project_start_date")),
            project_end=_dt(latest.get("project_end_date")),
            total_award_in_tenure=sum(int(x.get("award_amount") or 0) for x in rs),
            is_contact_pi=me.get("is_contact_pi"),
            is_subproject=any(x.get("subproject_id") for x in rs),
        ))
    return records, mode


def derive_grant_titles(records: list[GrantRecord]) -> list[str]:
    eligible = [r for r in records if (r.activity_code or "") in LLM_ACTIVITY_CODES]
    eligible.sort(key=lambda r: (r.last_fy or 0), reverse=True)
    return [r.title for r in eligible]
