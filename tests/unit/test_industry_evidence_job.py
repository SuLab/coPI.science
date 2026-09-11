import pytest
from sqlalchemy import select

from src.models import (
    Job,
    PiIndustryEvidence,
    PiIndustryScore,
    Publication,
    ResearcherProfile,
    User,
)
from src.services import industry_evidence as ie
from src.services.jhu_rules import set_tenure_start

pytestmark = pytest.mark.integration


async def test_no_tenure_start_writes_unscored_row_and_collects_nothing(db_session, monkeypatch):
    u = User(orcid="0000-0001-1111-2222", name="No Tenure", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()
    called = []

    async def boom(*a, **k):
        called.append(1)
        return []

    monkeypatch.setattr(ie, "fetch_works_for_pmids", boom)
    await ie.execute_industry_evidence(job, db_session)
    s = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id))).scalar_one()
    assert s.score is None and s.reason == "no_tenure_start" and called == []


async def test_job_stores_evidence_and_score(db_session, monkeypatch):
    u = User(orcid="0000-0002-2214-0114", name="Gyanu Lamichhane", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id, keywords=["tuberculosis"], disease_areas=["Tuberculosis"]))
    db_session.add(Publication(user_id=u.id, pmid="38980071", title="p", year=2024))
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()

    async def works(pmids):
        return [{"id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/38980071"}, "publication_year": 2024, "funders": [],
                 "authorships": [{"author": {"orcid": "https://orcid.org/0000-0002-2214-0114"}, "institutions": [{"id": "https://openalex.org/I145311948", "type": "education"}], "author_position": "last", "is_corresponding": True},
                                 {"author": {"orcid": None}, "institutions": [{"id": "https://openalex.org/I4210091798", "display_name": "Paratek Pharmaceuticals (United States)", "type": "company"}], "author_position": "middle"}]}]

    async def recs(pmids):
        return [{"pmid": "38980071", "year": 2024, "coi_statement": "A and B are employees of Paratek Pharmaceuticals, Inc.", "affiliations": []}]

    async def none(*a, **k):
        return []

    async def cf(ids):
        return set()

    monkeypatch.setattr(ie, "fetch_works_for_pmids", works)
    monkeypatch.setattr(ie, "fetch_pubmed_records", recs)
    monkeypatch.setattr(ie, "fetch_jhu_applications", none)
    monkeypatch.setattr(ie, "fetch_jhu_industry_trials", none)
    monkeypatch.setattr(ie, "company_funder_ids", cf)

    await ie.execute_industry_evidence(job, db_session)
    rows = (await db_session.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == u.id))).scalars().all()
    assert {r.kind for r in rows} == {"coauthor_company", "coi_relationship"}
    s = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id).order_by(PiIndustryScore.computed_at.desc()))).scalars().first()
    assert s.reason == "ok" and s.raw_sum == 4.5 + 5 and s.evidence_count == 2 and s.tenure_start_used == 2018
