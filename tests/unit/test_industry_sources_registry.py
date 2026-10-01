import pytest
from sqlalchemy import select

from src.models import Job, PiIndustryEvidence, User
from src.services import industry_evidence, industry_score
from src.services.industry_sources import registry
from src.services.industry_sources.registry import SOURCES, SourceUnavailable
from src.services.jhu_rules import set_tenure_start
from src.worker.main import JobContext

_TODAY_WEIGHTS = {
    "coauthor_company": (3.0, 30.0),
    "company_funder": (4.0, 20.0),
    "coi_relationship": (5.0, 25.0),
    "patent_filed": (4.0, 12.0),
    "patent_assigned": (10.0, 30.0),
    "trial_industry_collab": (3.0, 9.0),
    "sbir_sttr": (6.0, 12.0),
}


def test_weights_equal_todays_exactly():
    assert industry_score.WEIGHTS == _TODAY_WEIGHTS
    assert registry.weights() == _TODAY_WEIGHTS


def test_every_kind_has_exactly_one_source():
    kinds = [k for s in SOURCES for k in s.kinds]
    assert sorted(kinds) == sorted(_TODAY_WEIGHTS)


def test_source_names_match_the_evidence_source_column():
    assert {s.name for s in SOURCES} == {"openalex", "pubmed", "uspto", "ctgov", "nih_reporter"}


def _row(user_id, source, kind, external_id):
    return PiIndustryEvidence(user_id=user_id, source=source, kind=kind, external_id=external_id,
                              company_name="Acme", company_class="pharma_biotech", year=2020,
                              pi_role=None, in_tenure=True, evidence={})


@pytest.mark.integration
@pytest.mark.usefixtures("progress_on_test_connection")
async def test_a_failing_source_keeps_its_rows_and_others_refresh(db_session, monkeypatch):
    user = User(orcid="0000-0001-4242-0001", name="Isolation PI", user_role="pi")
    db_session.add(user)
    await db_session.flush()
    await set_tenure_start(user.id, 2015, "manual", db=db_session)
    db_session.add(_row(user.id, "ctgov", "trial_industry_collab", "NCT1:acme"))
    db_session.add(_row(user.id, "openalex", "coauthor_company", "W1:acme"))
    job = Job(type="industry_evidence", user_id=user.id,
              payload={"user_id": str(user.id), "orcid": user.orcid})
    db_session.add(job)
    await db_session.flush()

    async def fail(self, ctx):
        raise SourceUnavailable("ctgov down")

    async def empty(self, ctx):
        return registry.SourceResult(items=[])

    monkeypatch.setattr(registry.CtGovSource, "fetch", fail)
    for cls in (registry.OpenAlexSource, registry.PubMedCoiSource,
                registry.UsptoSource, registry.NihReporterSource):
        monkeypatch.setattr(cls, "fetch", empty)

    ctx = JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                     attempts=job.attempts, max_attempts=job.max_attempts)
    await industry_evidence.execute_industry_evidence(ctx, db_session)
    await db_session.refresh(job)
    rows = (await db_session.execute(select(PiIndustryEvidence.source)
                                     .where(PiIndustryEvidence.user_id == user.id))).scalars().all()
    assert rows == ["ctgov"]
    detail = [p for p in (job.payload or {}).get("progress", []) if p["step"] == "industry_done"]
    assert "unavailable=ctgov" in str(detail)


@pytest.mark.integration
@pytest.mark.usefixtures("progress_on_test_connection")
async def test_every_source_unavailable_fails_the_job_for_a_retry(db_session, monkeypatch):
    user = User(orcid="0000-0001-4242-0002", name="Outage PI", user_role="pi")
    db_session.add(user)
    await db_session.flush()
    await set_tenure_start(user.id, 2015, "manual", db=db_session)
    db_session.add(_row(user.id, "ctgov", "trial_industry_collab", "NCT2:acme"))
    job = Job(type="industry_evidence", user_id=user.id,
              payload={"user_id": str(user.id), "orcid": user.orcid})
    db_session.add(job)
    await db_session.flush()

    async def fail(self, ctx):
        raise SourceUnavailable("down")

    for cls in (registry.OpenAlexSource, registry.PubMedCoiSource, registry.UsptoSource,
                registry.CtGovSource, registry.NihReporterSource):
        monkeypatch.setattr(cls, "fetch", fail)
    ctx = JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                     attempts=job.attempts, max_attempts=job.max_attempts)
    with pytest.raises(SourceUnavailable, match="every industry source unavailable"):
        await industry_evidence.execute_industry_evidence(ctx, db_session)
    rows = (await db_session.execute(select(PiIndustryEvidence.source)
                                     .where(PiIndustryEvidence.user_id == user.id))).scalars().all()
    assert rows == ["ctgov"]
