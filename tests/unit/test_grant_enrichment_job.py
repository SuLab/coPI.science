import pytest
from sqlalchemy import select

from src.models import Job, PiGrant, Publication, ResearcherProfile, User
from src.services import grant_enrichment as ge
from src.services.jhu_rules import set_tenure_start

pytestmark = pytest.mark.integration
JHU = {"org_name": "JOHNS HOPKINS UNIVERSITY"}


def _row(core, fy, pid, org=JHU):
    return {"core_project_num": core, "project_num": f"5{core}-0{fy % 10}", "fiscal_year": fy, "organization": org,
            "award_amount": 10, "subproject_id": None, "activity_code": core[:3], "project_title": f"T {core}",
            "phr_text": None, "terms": None, "agency_ic_admin": {"code": "AI"}, "funding_mechanism": "Non-SBIR/STTR",
            "project_start_date": None, "project_end_date": None,
            "principal_investigators": [{"profile_id": pid, "is_contact_pi": True, "first_name": "Gyanu", "last_name": "Lamichhane"}]}


async def test_job_writes_only_pmid_linked_in_tenure_grants_and_projects_titles(db_session, monkeypatch):
    u = User(orcid="0000-0002-2214-0114", name="Gyanu Lamichhane", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id, grant_titles=["old orcid title"]))
    db_session.add(Publication(user_id=u.id, pmid="34187885", title="p"))
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    job = Job(type="enrich_grants", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()

    calls = []

    async def fake_search(criteria, fields, max_total=500):
        calls.append(criteria)
        if "pi_names" in criteria:
            return [_row("R01AI137329", 2019, 9751245), _row("R01ZZ000009", 2019, 555)]
        return [_row("R01AI137329", 2017, 9751245), _row("R01AI137329", 2019, 9751245), _row("R21AI190702", 2026, 9751245)]

    async def fake_links(cores):
        return {"R01AI137329": {"34187885"}, "R01ZZ000009": {"1"}}

    monkeypatch.setattr(ge, "search_projects", fake_search)
    monkeypatch.setattr(ge, "publications_for_cores", fake_links)

    await ge.execute_enrich_grants(job, db_session)

    grants = (await db_session.execute(select(PiGrant).where(PiGrant.user_id == u.id))).scalars().all()
    assert {g.core_project_num for g in grants} == {"R01AI137329", "R21AI190702"}
    r01 = next(g for g in grants if g.core_project_num == "R01AI137329")
    assert r01.first_fy == 2019 and r01.tenure_filter_mode == "org_and_year" and r01.identity_evidence["pmid_linked"] == [9751245]
    prof = (await db_session.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == u.id))).scalar_one()
    assert prof.grant_titles == ["T R21AI190702", "T R01AI137329"]
    assert calls[1]["org_names_exact_match"] == ["JOHNS HOPKINS UNIVERSITY"] and calls[1]["pi_profile_ids"] == [9751245]


async def test_no_candidates_completes_without_rows(db_session, monkeypatch):
    u = User(orcid="0000-0001-0000-0001", name="Nobody Here", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    job = Job(type="enrich_grants", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()

    async def none(*a, **k):
        return []

    monkeypatch.setattr(ge, "search_projects", none)
    await ge.execute_enrich_grants(job, db_session)
    assert (await db_session.execute(select(PiGrant).where(PiGrant.user_id == u.id))).scalars().all() == []
    progress = job.payload.get("progress") or []
    assert progress and "no_reporter_match" in progress[-1]["detail"]
