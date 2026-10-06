import httpx
import pytest
import respx
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
from src.services import pubmed
from src.services.industry_score import SCORER_VERSION
from src.services.industry_sources import Paged, ctgov, openalex_industry, uspto_inventor
from src.services.jhu_rules import set_tenure_start
from src.worker.main import JobContext

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]

LAM = {"last": "Lamichhane", "fore": "Gyanu", "initials": "G", "collective": None}
DECK = {"last": "Deck", "fore": "Daniel H", "initials": "DH", "collective": None}
SERIO = {"last": "Serio", "fore": "Alisa W", "initials": "AW", "collective": None}

_PARATEK_WORK = {
    "id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/38980071"},
    "publication_year": 2024, "funders": [],
    "authorships": [
        {"author": {"orcid": "https://orcid.org/0000-0002-2214-0114"},
         "institutions": [{"id": "https://openalex.org/I145311948", "type": "education"}],
         "author_position": "last", "is_corresponding": True},
        {"author": {"orcid": None},
         "institutions": [{"id": "https://openalex.org/I4210091798",
                           "display_name": "Paratek Pharmaceuticals (United States)",
                           "type": "company"}],
         "author_position": "middle"},
    ],
}


def _ctx(job):
    return JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                      attempts=job.attempts, max_attempts=job.max_attempts)


def _attributed_founder(user_id, external_id="k1"):
    return PiIndustryEvidence(
        user_id=user_id, source="pubmed", kind="coi_relationship", external_id=external_id,
        company_name="Startup Inc", company_class="pharma_biotech", year=2021, pi_role=None,
        in_tenure=True, evidence={"relationship": "founder", "attributed": True, "pi_mention": "G.L."})


async def _user(db_session, orcid, name):
    u = User(orcid=orcid, name=name, user_role="pi")
    db_session.add(u)
    await db_session.flush()
    return u


async def _lamichhane_job(db_session, monkeypatch, *, authors, coi_statement):
    """The Lamichhane PI with one 2024 paper, a tenure start of 2018, and stand-ins for
    every upstream: OpenAlex returns the Paratek co-authored work, PubMed one record with
    `authors` and `coi_statement`, USPTO and ClinicalTrials.gov nothing."""
    u = await _user(db_session, "0000-0002-2214-0114", "Gyanu Lamichhane")
    db_session.add(ResearcherProfile(user_id=u.id, keywords=["tuberculosis"], disease_areas=["Tuberculosis"]))
    db_session.add(Publication(user_id=u.id, pmid="38980071", title="p", year=2024))
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()

    async def works(pmids):
        return Paged([_PARATEK_WORK])

    async def recs(pmids, **kw):
        return [{"pmid": "38980071", "year": 2024, "authors": authors, "coi_statement": coi_statement}]

    async def none(*a, **k):
        return Paged([])

    async def cf(ids):
        return set()

    monkeypatch.setattr(openalex_industry, "fetch_works_for_pmids", works)
    monkeypatch.setattr(pubmed, "fetch_pubmed_records", recs)
    monkeypatch.setattr(uspto_inventor, "fetch_jhu_applications", none)
    monkeypatch.setattr(ctgov, "fetch_jhu_industry_trials", none)
    monkeypatch.setattr(openalex_industry, "company_funder_ids", cf)
    return u, job


async def test_no_tenure_start_writes_an_unscored_row_and_collects_nothing(db_session, monkeypatch):
    u = await _user(db_session, "0000-0001-1111-2222", "No Tenure")
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()
    called = []

    async def boom(*a, **k):
        called.append(1)
        return Paged([])

    monkeypatch.setattr(openalex_industry, "fetch_works_for_pmids", boom)
    await ie.execute_industry_evidence(_ctx(job), db_session)
    s = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id))).scalar_one()
    assert s.tenure_start_used is None and s.coverage == {} and s.scorer_version == SCORER_VERSION
    assert s.score is None and s.reason is None and called == []


async def test_job_stores_attributed_evidence_and_a_score_row(db_session, monkeypatch):
    u, job = await _lamichhane_job(
        db_session, monkeypatch, authors=[LAM],
        coi_statement="G.L. is an employee of Paratek Pharmaceuticals, Inc.")
    await ie.execute_industry_evidence(_ctx(job), db_session)
    rows = (await db_session.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == u.id))).scalars().all()
    assert {r.kind for r in rows} == {"coauthor_company", "coi_relationship"}
    coi = next(r for r in rows if r.kind == "coi_relationship")
    assert coi.evidence["attributed"] is True and coi.evidence["pi_mention"] == "G.L."
    s = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == u.id))).scalar_one()
    assert s.raw_sum == 4.5 + 5 and s.evidence_count == 2 and s.tenure_start_used == 2018
    assert s.coverage == {"openalex": "ok", "pubmed": "ok", "uspto": "ok", "ctgov": "ok", "nih_reporter": "ok"}
    assert s.score is None and s.reason is None and s.primary_field is None


async def test_the_paratek_statement_credits_nothing(db_session, monkeypatch):
    """Inverted end to end (P4): "A and B are employees of Paratek…" names other people."""
    u, job = await _lamichhane_job(
        db_session, monkeypatch, authors=[DECK, SERIO, LAM],
        coi_statement="Daniel H. Deck and Alisa W. Serio are employees of Paratek Pharmaceuticals, Inc.")
    await ie.execute_industry_evidence(_ctx(job), db_session)
    rows = (await db_session.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == u.id))).scalars().all()
    assert {r.kind for r in rows} == {"coauthor_company"}


async def test_rescore_with_no_evidence_records_a_zero_raw(db_session):
    u = await _user(db_session, "0000-0003-3333-4444", "No Evidence")
    s = await ie.rescore_user(db_session, u.id, tenure_start=2018)
    assert s.raw_sum == 0.0 and s.evidence_count == 0 and s.score is None and s.reason is None


async def test_rescore_counts_an_attributed_founder_twice(db_session):
    u = await _user(db_session, "0000-0004-4444-5555", "Founder PI")
    db_session.add(_attributed_founder(u.id))
    await db_session.flush()
    s = await ie.rescore_user(db_session, u.id, tenure_start=2018)
    assert s.raw_sum == 10.0 and s.evidence_count == 1


async def test_live_evidence_that_scores_zero_has_a_zero_raw(db_session):
    """Ported from the fix wave (0fb613c): a class-gated cro_vendor co-author is live
    evidence with raw 0.0, which read time calls no_evidence (Task 5's test)."""
    u = await _user(db_session, "0000-0007-7777-8888", "Vendor PI")
    db_session.add(PiIndustryEvidence(user_id=u.id, source="openalex", kind="coauthor_company",
                                      external_id="W1:I1", company_name="Applied BioPhysics",
                                      company_class="cro_vendor", year=2021, pi_role="last",
                                      in_tenure=True, evidence={}))
    await db_session.flush()
    s = await ie.rescore_user(db_session, u.id, tenure_start=2018)
    assert s.raw_sum == 0.0 and s.evidence_count == 1


async def test_rescore_re_reads_tenure_start_when_omitted(db_session):
    u = await _user(db_session, "0000-0006-6666-7777", "Rescored PI")
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    s = await ie.rescore_user(db_session, u.id)
    assert s.tenure_start_used == 2018


@respx.mock
async def test_the_job_completes_when_uspto_answers_404(db_session, monkeypatch):
    u = await _user(db_session, "0000-0005-5555-6666", "No Patents")
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    job = Job(type="industry_evidence", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()

    async def none(*a, **k):
        return []

    async def no_pages(*a, **k):
        return Paged([])

    async def cf(ids):
        return set()

    monkeypatch.setattr(openalex_industry, "fetch_works_for_pmids", no_pages)
    monkeypatch.setattr(pubmed, "fetch_pubmed_records", none)
    monkeypatch.setattr(ctgov, "fetch_jhu_industry_trials", no_pages)
    monkeypatch.setattr(openalex_industry, "company_funder_ids", cf)
    monkeypatch.setattr(
        "src.services.industry_sources.uspto_inventor.get_settings",
        lambda: type("S", (), {"uspto_api_key": "k"})(),
    )
    respx.post(uspto_inventor.SEARCH_URL).mock(return_value=httpx.Response(404))

    await ie.execute_industry_evidence(_ctx(job), db_session)  # must not raise
    await db_session.refresh(job)
    progress = [p["step"] for p in (job.payload or {}).get("progress", [])]
    assert "industry_done" in progress
