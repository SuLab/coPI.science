"""Turn RePORTER rows into per-PI grant records: identity, tenure, collapse.

Identity rule (spec 2026-10-05 §6.1; P1, E-1, D2, D56):
1. Stage 1 searches `pi_names` with each surname candidate of the PI's name
   (`person_names.surname_candidates`) at JOHNS HOPKINS UNIVERSITY exactly; a total over
   the cap is `firehose`.
2. A candidate is a PI entry on a JHU row whose `last_name` shares a key with the PI's
   `surname_keys` and whose `first_name` agrees (`given_names_agree`). Co-PIs on the same
   award are not candidates: a shared core no longer lends its PMIDs to every name on it.
3. A candidate is accepted when one of ITS OWN cores links (RePORTER publications
   endpoint) to a PMID stored for the PI, pre-tenure rows included (D62a).
4. One accepted: `resolved`; more than one: `held`; none accepted and exactly one
   candidate: `unconfirmed` (D56); otherwise `no_match`. A staff pin overrides all of
   these (`pinned`, `none_confirmed`); a stage-2 total over the cap is `firehose`. Only
   `resolved` and `pinned` render RePORTER grants (D9).

Tenure rule (B6-B8): a fiscal-year row counts iff org == JHU exact AND fiscal_year >=
tenure_start (`jhu_rules.export_tenure_start`). With no tenure start the year half is
skipped and the mode is `org_only`; the persona then shows Active Grants only (D57).
"""
from dataclasses import dataclass
from datetime import UTC, datetime

from src.services.nih_reporter import JHU_ORG_EXACT
from src.services.person_names import PersonName, given_names_agree, surname_keys

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
    #: "First Last" as RePORTER spells the PI's entry on the latest fiscal-year row.
    name_on_award: str | None = None


@dataclass(frozen=True)
class Candidate:
    profile_id: int
    name_on_award: str
    cores: frozenset[str]
    linking_pmids: frozenset[str] = frozenset()

    def as_json(self) -> dict:
        """The `pi_grant_identity.candidates` element (id, name on award, linked, linking PMIDs)."""
        return {"id": self.profile_id, "name_on_award": self.name_on_award,
                "linked": bool(self.linking_pmids), "linking_pmids": sorted(self.linking_pmids)}


@dataclass(frozen=True)
class IdentityResult:
    status: str                         # resolved | held | unconfirmed | no_match
    accepted_profile_ids: tuple[int, ...]
    candidates: tuple[Candidate, ...]


def _org(row: dict) -> str:
    return ((row.get("organization") or {}).get("org_name") or "").strip().upper()


def _pis(row: dict) -> list[dict]:
    return row.get("principal_investigators") or []


def award_name(pi_entry: dict) -> str:
    """"First Last" as RePORTER spells this PI entry."""
    parts = ((pi_entry.get("first_name") or "").strip(), (pi_entry.get("last_name") or "").strip())
    return " ".join(p for p in parts if p)


def names_the_pi(pi_entry: dict, person: PersonName) -> bool:
    """Step 2's name test: a shared surname key and agreeing given names."""
    return bool(surname_keys(pi_entry.get("last_name") or "") & person.surname_keys) and \
        given_names_agree(pi_entry.get("first_name") or "", person.first)


def find_candidates(rows: list[dict], person: PersonName) -> dict[int, Candidate]:
    """Name-matched PI entries on JHU rows, by profile id, with their own cores (step 2)."""
    names: dict[int, str] = {}
    cores: dict[int, set[str]] = {}
    for row in rows:
        if _org(row) != JHU_ORG_EXACT:
            continue
        for entry in _pis(row):
            pid = entry.get("profile_id")
            if pid is None or not names_the_pi(entry, person):
                continue
            names.setdefault(pid, award_name(entry))
            cores.setdefault(pid, set()).add(row["core_project_num"])
    return {pid: Candidate(pid, names[pid], frozenset(cores[pid])) for pid in sorted(cores)}


def resolve_identity(candidates: dict[int, Candidate], links: dict[str, set[str]],
                     corpus_pmids: set[str]) -> IdentityResult:
    """Steps 3-4: link each candidate through its own cores, then name the status."""
    linked = []
    for pid in sorted(candidates):
        c = candidates[pid]
        pmids = set().union(*(links.get(core, set()) for core in c.cores)) & corpus_pmids
        linked.append(Candidate(c.profile_id, c.name_on_award, c.cores, frozenset(pmids)))
    accepted = tuple(c.profile_id for c in linked if c.linking_pmids)
    if len(accepted) == 1:
        status = "resolved"
    elif accepted:
        status = "held"
    elif len(linked) == 1:
        status = "unconfirmed"
    else:
        status = "no_match"
    return IdentityResult(status, accepted, tuple(linked))


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
            name_on_award=award_name(me) if me else None,
        ))
    return records, mode


def grant_evidence(record: GrantRecord, links: dict[str, set[str]], corpus_pmids: set[str],
                   rule: str) -> dict:
    """`pi_grants.identity_evidence` (P12): what ties THIS award to the PI. `rule` is
    `pmid_link` (resolved through the corpus) or `pinned` (a staff pin)."""
    return {
        "matched_profile_id": record.reporter_profile_id,
        "name_on_award": record.name_on_award,
        "linking_pmids": sorted(links.get(record.core_project_num, set()) & corpus_pmids),
        "rule": rule,
    }

