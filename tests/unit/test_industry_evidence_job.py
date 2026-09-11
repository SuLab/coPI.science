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
from src.services.industry_score import SCORER_VERSION
from src.services.jhu_rules import set_tenure_start

pytestmark = pytest.mark.integration


async def _seed_peers(db_session, n, raw_sum=1.0):
    for i in range(n):
        peer = User(orcid=f"0000-0009-9999-{i:04d}", name=f"Peer {i}", user_role="pi")
        db_session.add(peer)
        await db_session.flush()
        db_session.add(PiIndustryScore(user_id=peer.id, score=50.0, raw_sum=raw_sum + i, reason="ok",
                                        components={}, evidence_count=1, scorer_version=SCORER_VERSION))
    await db_session.flush()


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
    await _seed_peers(db_session, 3)
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
    assert s.score is not None


async def test_rescore_with_no_evidence_is_unscored(db_session):
    u = User(orcid="0000-0003-3333-4444", name="No Evidence", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    s = await ie.rescore_user(db_session, u.id, tenure_start=2018)
    assert s.score is None and s.reason == "no_evidence" and s.raw_sum == 0.0


async def test_rescore_with_evidence_but_no_peers_is_cohort_too_small(db_session):
    u = User(orcid="0000-0004-4444-5555", name="Lonely PI", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    db_session.add(PiIndustryEvidence(user_id=u.id, source="pubmed", kind="coi_relationship", external_id="k1",
                                       company_name="Startup Inc", company_class="pharma_biotech", year=2021,
                                       pi_role=None, in_tenure=True, evidence={"relationship": "founder"}))
    await db_session.flush()
    s = await ie.rescore_user(db_session, u.id, tenure_start=2018)
    assert s.score is None and s.reason == "cohort_too_small" and s.raw_sum > 0


async def test_rescore_with_evidence_and_enough_peers_is_scored(db_session):
    u = User(orcid="0000-0005-5555-6666", name="Popular PI", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    db_session.add(PiIndustryEvidence(user_id=u.id, source="pubmed", kind="coi_relationship", external_id="k1",
                                       company_name="Startup Inc", company_class="pharma_biotech", year=2021,
                                       pi_role=None, in_tenure=True, evidence={"relationship": "founder"}))
    await _seed_peers(db_session, 3)
    s = await ie.rescore_user(db_session, u.id, tenure_start=2018)
    assert s.reason == "ok" and s.score is not None and s.raw_sum == 10.0


async def test_rescore_re_reads_tenure_start_when_omitted(db_session):
    u = User(orcid="0000-0006-6666-7777", name="Rescored PI", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    s = await ie.rescore_user(db_session, u.id)
    assert s.tenure_start_used == 2018
