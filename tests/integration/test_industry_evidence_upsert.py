"""industry_evidence's writes (spec 2026-10-05 §6.2, E-9, E-13, NEW-1): rows are upserted
with their id and veto kept; only a source whose coverage is ok deletes the rows it did
not return; long keys are hashed; each score row carries the run's coverage and a
clock_timestamp() computed_at."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models import Job, PiIndustryEvidence, PiIndustryScore, User
from src.services import industry_evidence as ie
from src.services.industry_sources import EvidenceItem, registry
from src.services.industry_sources.registry import SourceResult, SourceUnavailable
from src.services.jhu_rules import set_tenure_start
from src.worker.main import JobContext

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]


def _item(source, kind, external_id, company="Acme Therapeutics", year=2021):
    return EvidenceItem(source, kind, external_id, company, None, "pharma_biotech", year, "last", True, {})


def _answer(monkeypatch, results: dict) -> None:
    """Each source's fetch answers results[name] (a SourceResult or an exception to raise);
    a source not named answers an empty, ok result."""
    for source in registry.SOURCES:
        outcome = results.get(source.name, SourceResult())

        async def fetch(self, ctx, _outcome=outcome):
            if isinstance(_outcome, BaseException):
                raise _outcome
            return _outcome

        monkeypatch.setattr(type(source), "fetch", fetch)


async def _pi(db_session, tenure=2015) -> tuple[User, JobContext]:
    user = User(orcid=f"0000-0007-{uuid.uuid4().hex[:4]}-0001", name="Upsert PI", user_role="pi")
    db_session.add(user)
    await db_session.flush()
    await set_tenure_start(user.id, tenure, "manual", db=db_session)
    job = Job(type="industry_evidence", user_id=user.id, payload={"user_id": str(user.id), "orcid": user.orcid})
    db_session.add(job)
    await db_session.flush()
    return user, JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                            attempts=0, max_attempts=3)


def _stored(source, kind, external_id, user_id, *, vetoed=False, company="Old Name", year=2021):
    return PiIndustryEvidence(user_id=user_id, source=source, kind=kind, external_id=external_id,
                              company_name=company, company_class="pharma_biotech", year=year,
                              pi_role="last", in_tenure=True, evidence={},
                              vetoed_at=datetime.now(UTC) if vetoed else None)


async def _rows(db_session, user_id) -> dict[str, PiIndustryEvidence]:
    rows = (await db_session.execute(
        select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id)
        .execution_options(populate_existing=True))).scalars().all()
    return {r.external_id: r for r in rows}


async def test_upsert_keeps_ids_and_vetoes(db_session, monkeypatch):
    user, ctx = await _pi(db_session)
    vetoed = _stored("openalex", "coauthor_company", "W1:I1", user.id, vetoed=True)
    live = _stored("openalex", "coauthor_company", "W2:I2", user.id)
    db_session.add_all([vetoed, live])
    await db_session.flush()
    ids = {vetoed.external_id: vetoed.id, live.external_id: live.id}
    _answer(monkeypatch, {"openalex": SourceResult([
        _item("openalex", "coauthor_company", "W1:I1", company="New Name"),
        _item("openalex", "coauthor_company", "W2:I2", company="New Name"),
    ])})
    await ie.execute_industry_evidence(ctx, db_session)
    rows = await _rows(db_session, user.id)
    assert {k: r.id for k, r in rows.items()} == ids
    assert rows["W1:I1"].vetoed_at is not None and rows["W1:I1"].company_name == "New Name"
    assert rows["W2:I2"].vetoed_at is None and rows["W2:I2"].company_name == "New Name"


async def test_an_ok_source_deletes_unreturned_rows_but_keeps_vetoed_ones(db_session, monkeypatch):
    user, ctx = await _pi(db_session)
    db_session.add_all([
        _stored("pubmed", "coi_relationship", "9:johns hopkins co", user.id),            # NEW-1 junk
        _stored("pubmed", "coi_relationship", "9:vetoed inc", user.id, vetoed=True),
    ])
    await db_session.flush()
    _answer(monkeypatch, {})
    await ie.execute_industry_evidence(ctx, db_session)
    assert set(await _rows(db_session, user.id)) == {"9:vetoed inc"}


async def test_truncated_and_unavailable_sources_keep_unreturned_rows(db_session, monkeypatch):
    user, ctx = await _pi(db_session)
    db_session.add_all([
        _stored("uspto", "patent_assigned", "APP1:acme", user.id),
        _stored("ctgov", "trial_industry_collab", "NCT1:acme", user.id),
    ])
    await db_session.flush()
    _answer(monkeypatch, {
        "uspto": SourceResult([], coverage="truncated"),
        "ctgov": SourceUnavailable("ctgov: 503", reason="http_503"),
    })
    await ie.execute_industry_evidence(ctx, db_session)
    assert set(await _rows(db_session, user.id)) == {"APP1:acme", "NCT1:acme"}
    score = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == user.id))).scalar_one()
    assert score.coverage == {"openalex": "ok", "pubmed": "ok", "uspto": "truncated",
                              "ctgov": "unavailable:http_503", "nih_reporter": "ok"}


async def test_kept_rows_score_only_from_the_tenure_start(db_session, monkeypatch):
    user, ctx = await _pi(db_session, tenure=2015)
    db_session.add_all([
        _stored("ctgov", "trial_industry_collab", "NCT1:old", user.id, company="Old Pharma", year=2012),
        _stored("ctgov", "trial_industry_collab", "NCT2:new", user.id, company="New Pharma", year=2020),
    ])
    await db_session.flush()
    _answer(monkeypatch, {"ctgov": SourceUnavailable("ctgov down")})
    await ie.execute_industry_evidence(ctx, db_session)
    score = (await db_session.execute(select(PiIndustryScore).where(PiIndustryScore.user_id == user.id))).scalar_one()
    assert score.raw_sum == 4.5      # trial_industry_collab 3.0 x role "last" 1.5; the 2012 row adds nothing


async def test_long_external_ids_are_hashed_and_stable(db_session, monkeypatch):
    user, ctx = await _pi(db_session)
    long_id = "NCT1:" + "a very long collaborator name " * 8
    _answer(monkeypatch, {"ctgov": SourceResult([_item("ctgov", "trial_industry_collab", long_id)])})
    await ie.execute_industry_evidence(ctx, db_session)
    (first,) = (await _rows(db_session, user.id)).values()
    assert len(first.external_id) == 117 and first.external_id == ie.evidence_key(long_id)
    await ie.execute_industry_evidence(ctx, db_session)
    (again,) = (await _rows(db_session, user.id)).values()
    assert again.id == first.id


async def test_duplicate_keys_in_one_batch_are_written_once(db_session, monkeypatch):
    user, ctx = await _pi(db_session)
    same = _item("openalex", "coauthor_company", "W1:I1")
    _answer(monkeypatch, {"openalex": SourceResult([same, same])})
    await ie.execute_industry_evidence(ctx, db_session)    # no CardinalityViolation
    assert set(await _rows(db_session, user.id)) == {"W1:I1"}


async def test_score_rows_take_clock_timestamp_and_no_read_time_fields(db_session):
    user, _ = await _pi(db_session)
    first = await ie.rescore_user(db_session, user.id, coverage={"openalex": "ok"})
    second = await ie.rescore_user(db_session, user.id, coverage={"openalex": "ok"})
    assert second.computed_at > first.computed_at        # now() would tie inside one transaction
    assert (first.score, first.reason, first.field_percentile, first.primary_field) == (None, None, None, None)


async def test_a_veto_rescore_copies_the_latest_coverage(db_session):
    user, _ = await _pi(db_session)
    await ie.rescore_user(db_session, user.id, coverage={"uspto": "truncated"})
    again = await ie.rescore_user(db_session, user.id)
    assert again.coverage == {"uspto": "truncated"}


async def test_a_rescore_before_the_first_run_marks_every_source_not_refreshed(db_session):
    """A veto before the PI's first 2.0.0 job: the row shows partial and counts as not
    refreshed (A5)."""
    user, _ = await _pi(db_session)
    score = await ie.rescore_user(db_session, user.id)
    assert score.coverage == {s.name: ie.NOT_REFRESHED for s in registry.SOURCES}


async def test_a_rescore_after_a_no_tenure_run_marks_every_source_not_refreshed(db_session):
    """A 2.0.0 run with no tenure start refreshed no source (coverage {}); a veto after
    staff set a tenure year must not read that as full coverage (plan audit 2026-10-06)."""
    user, _ = await _pi(db_session)
    await ie.rescore_user(db_session, user.id, coverage={})
    again = await ie.rescore_user(db_session, user.id)
    assert again.coverage == {s.name: ie.NOT_REFRESHED for s in registry.SOURCES}


async def test_only_the_producerless_source_answering_is_an_outage(db_session, monkeypatch):
    """nih_reporter has no producer; its "ok" must not hide the other four failing."""
    _user, ctx = await _pi(db_session)
    _answer(monkeypatch, {name: SourceUnavailable(f"{name} down")
                          for name in ("openalex", "pubmed", "uspto", "ctgov")})
    with pytest.raises(SourceUnavailable, match="every industry source unavailable"):
        await ie.execute_industry_evidence(ctx, db_session)


async def test_evidence_key_leaves_short_keys_alone():
    assert ie.evidence_key("W1:I1") == "W1:I1"
    assert ie.evidence_key("x" * 120) == "x" * 120
    assert len(ie.evidence_key("x" * 121)) == 117
