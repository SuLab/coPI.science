"""Excluded rows are ignored everywhere they could reach a PI's persona or a score
(spec 2026-10-05 §6.3, D45)."""
from datetime import UTC, datetime

import pytest

from src.models import Job, Publication
from src.services import company_discovery
from src.services import grant_enrichment as ge
from src.services import industry_evidence as ie
from src.services.jhu_rules import set_tenure_start
from src.services.tenure_scope import scoped_counts, scoped_publications_for_export
from src.worker.main import JobContext
from tests import factories

pytestmark = pytest.mark.integration


class _Captured(Exception):
    """Stops a job right after the read under test has been captured."""


async def _pi_with_rows(db_session, **user_kw):
    pi = await factories.make_user(db_session, **user_kw)
    db_session.add_all([
        Publication(user_id=pi.id, pmid="1", title="Kept", year=2020),
        Publication(user_id=pi.id, pmid="2", title="Excluded", year=2021,
                    excluded_at=datetime.now(UTC)),
    ])
    await db_session.flush()
    return pi


async def test_export_and_counts_skip_excluded_rows(db_session):
    pi = await _pi_with_rows(db_session)
    scoped = await scoped_publications_for_export(db_session, pi.id)
    assert [p.title for p in scoped.publications] == ["Kept"]
    assert (await scoped_counts(db_session, [pi.id]))[pi.id].in_tenure == 1


async def test_discovery_reads_no_excluded_pmid(db_session):
    pi = await _pi_with_rows(db_session)
    assert set(await company_discovery._publication_years(db_session, pi.id)) == {"1"}


async def test_reporter_linking_reads_no_excluded_pmid(db_session, monkeypatch):
    pi = await _pi_with_rows(db_session, name="Fidel Zavala")
    seen = {}

    async def no_rows(user):
        return []

    async def no_links(cores):
        return {}

    def capture(found, links, corpus):
        seen["corpus"] = set(corpus)
        raise _Captured

    monkeypatch.setattr(ge, "_stage1", no_rows)
    monkeypatch.setattr(ge, "find_candidates", lambda rows, person: {})
    monkeypatch.setattr(ge, "publications_for_cores", no_links)
    monkeypatch.setattr(ge, "resolve_identity", capture)
    with pytest.raises(_Captured):
        await ge.resolve_grants(db_session, pi, None)
    assert seen["corpus"] == {"1"}


async def test_the_industry_job_reads_no_excluded_pmid(db_session, monkeypatch):
    pi = await _pi_with_rows(db_session)
    await set_tenure_start(pi.id, 2018, "manual", db=db_session)
    job = Job(type="industry_evidence", user_id=pi.id,
              payload={"user_id": str(pi.id), "orcid": pi.orcid})
    db_session.add(job)
    await db_session.flush()
    seen = {}

    async def capture(ctx, sctx):
        seen["pmids"] = list(sctx.pmids)
        seen["years"] = dict(sctx.year_by_pmid)
        raise _Captured

    monkeypatch.setattr(ie, "_collect", capture)
    ctx = JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                     attempts=job.attempts, max_attempts=job.max_attempts)
    with pytest.raises(_Captured):
        await ie.execute_industry_evidence(ctx, db_session)
    assert seen["pmids"] == ["1"] and set(seen["years"]) == {"1"}


async def test_the_profile_page_lists_no_excluded_row(client, db_session):
    from tests.integration.test_manager_access import auth_headers

    pi = await _pi_with_rows(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.get("/profile", headers=auth_headers(pi.id))
    assert r.status_code == 200 and "Kept" in r.text and "Excluded" not in r.text
