"""The enrich_grants job: RePORTER identity, stored awards, ORCID fundings and the
post-commit persona write (spec 2026-10-05 §6.1)."""
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, text

from src.models import Job, PiGrant, PiGrantIdentity, PiOrcidFunding, ProfileRevision, Publication
from src.services import grant_enrichment as ge
from src.services import pi_companies, profile_export
from src.services.grant_resolution import GrantRecord
from src.services.jhu_rules import set_provisional_tenure_start, set_tenure_start
from src.services.nih_reporter import ReporterFirehoseError
from src.services.orcid_fundings import OrcidFunding
from src.worker.main import JobContext
from tests import factories

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]
JHU = {"org_name": "JOHNS HOPKINS UNIVERSITY"}


def _ctx(job):
    return JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                      attempts=job.attempts, max_attempts=job.max_attempts)


def _pi(pid, first="Fidel", last="Zavala"):
    return {"profile_id": pid, "is_contact_pi": True, "first_name": first, "last_name": last}


def _row(core, fy, pis, org=JHU, end="2099-06-30T00:00:00"):
    return {"core_project_num": core, "project_num": f"5{core}-0{fy % 10}", "fiscal_year": fy,
            "organization": org, "award_amount": 10, "subproject_id": None,
            "activity_code": core[:3], "project_title": f"T {core}", "phr_text": None,
            "terms": None, "agency_ic_admin": {"code": "AI"}, "funding_mechanism": "Non-SBIR/STTR",
            "project_start_date": None, "project_end_date": end, "principal_investigators": pis}


async def _setup(db_session, monkeypatch, *, name="Fidel Zavala", fundings=(), stage1=(),
                 stage2=(), links=None):
    u = await factories.make_user(db_session, name=name)
    db_session.add(Publication(user_id=u.id, pmid="34187885", title="p"))
    job = Job(type="enrich_grants", user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid})
    db_session.add(job)
    await db_session.flush()
    calls = []

    async def fake_search(criteria, fields, max_total=500):
        calls.append(criteria)
        result = stage1 if "pi_names" in criteria else stage2
        if isinstance(result, Exception):
            raise result
        return list(result)

    async def fake_links(cores):
        return dict(links or {})

    async def fake_fundings(orcid, *, strict):
        assert strict, "enrich_grants fetches ORCID strictly"
        if isinstance(fundings, Exception):
            raise fundings
        return list(fundings)

    monkeypatch.setattr(ge, "search_projects", fake_search)
    monkeypatch.setattr(ge, "publications_for_cores", fake_links)
    monkeypatch.setattr(ge, "fetch_orcid_fundings", fake_fundings)
    return u, job, calls


async def _grants(db_session, uid):
    return (await db_session.execute(select(PiGrant).where(PiGrant.user_id == uid))).scalars().all()


def _stale_and_vetoed(uid):
    return [
        PiGrant(user_id=uid, core_project_num="R01OLD00001", title="stale", org_name="X",
                tenure_filter_mode="org_only"),
        PiGrant(user_id=uid, core_project_num="R01VET00001", title="vetoed", org_name="X",
                tenure_filter_mode="org_only", vetoed_at=datetime.now(UTC)),
    ]


async def test_resolved_pi_stores_only_its_own_awards(db_session, monkeypatch):
    shared = _row("R01AI000001", 2020, [_pi(111), _pi(222, "Isabelle", "Coppens")])
    coppens_only = _row("R01AI000009", 2020, [_pi(222, "Isabelle", "Coppens")])
    u, job, calls = await _setup(db_session, monkeypatch, stage1=[shared, coppens_only],
                                 stage2=[shared], links={"R01AI000001": {"34187885"}})
    await set_tenure_start(u.id, 2018, "manual", db=db_session)
    await ge.execute_enrich_grants(_ctx(job), db_session)
    [g] = await _grants(db_session, u.id)
    assert g.core_project_num == "R01AI000001" and g.reporter_profile_id == 111
    assert g.identity_evidence == {"matched_profile_id": 111, "name_on_award": "Fidel Zavala",
                                   "linking_pmids": ["34187885"], "rule": "pmid_link"}
    assert calls[-1]["pi_profile_ids"] == [111] and calls[-1]["fiscal_years"][0] == 2018
    identity = await db_session.get(PiGrantIdentity, u.id)
    assert identity.status == "resolved" and identity.accepted_profile_ids == [111]
    assert identity.evaluated_at is not None and [c["id"] for c in identity.candidates] == [111]


async def test_unconfirmed_deletes_stale_rows_and_keeps_vetoed(db_session, monkeypatch):
    u, job, _ = await _setup(db_session, monkeypatch, stage1=[_row("R21CC000003", 2022, [_pi(333)])])
    db_session.add_all(_stale_and_vetoed(u.id))
    await db_session.flush()
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert [g.core_project_num for g in await _grants(db_session, u.id)] == ["R01VET00001"]
    assert (await db_session.get(PiGrantIdentity, u.id)).status == "unconfirmed"


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
async def test_firehose_at_either_stage(db_session, monkeypatch, stage):
    kwargs = {"stage1": ReporterFirehoseError("x")} if stage == "stage1" else {
        "stage1": [_row("R01AI000001", 2020, [_pi(111)])], "stage2": ReporterFirehoseError("x"),
        "links": {"R01AI000001": {"34187885"}}}
    u, job, _ = await _setup(db_session, monkeypatch, **kwargs)
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01OLD00001", title="stale",
                           org_name="X", tenure_filter_mode="org_only"))
    await db_session.flush()
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert await _grants(db_session, u.id) == []
    assert (await db_session.get(PiGrantIdentity, u.id)).status == "firehose"


async def test_a_pin_with_two_ids_skips_identity_and_fetches_both(db_session, monkeypatch):
    u, job, calls = await _setup(db_session, monkeypatch, stage2=[
        _row("R01AI000001", 2020, [_pi(111)]), _row("R01AI000002", 2021, [_pi(112)])])
    db_session.add(PiGrantIdentity(user_id=u.id, status="pinned", pinned_profile_ids=[112, 111],
                                   pinned_at=datetime.now(UTC)))
    await db_session.flush()
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert all("pi_names" not in c for c in calls) and calls[0]["pi_profile_ids"] == [111, 112]
    grants = await _grants(db_session, u.id)
    assert {g.reporter_profile_id for g in grants} == {111, 112}
    assert {g.identity_evidence["rule"] for g in grants} == {"pinned"}
    identity = await db_session.get(PiGrantIdentity, u.id)
    await db_session.refresh(identity)
    assert identity.status == "pinned" and identity.pinned_profile_ids == [112, 111]


async def test_none_confirmed_skips_reporter_and_clears_rows(db_session, monkeypatch):
    u, job, calls = await _setup(db_session, monkeypatch)
    db_session.add(PiGrantIdentity(user_id=u.id, status="none_confirmed", none_confirmed=True))
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01OLD00001", title="stale",
                           org_name="X", tenure_filter_mode="org_only"))
    await db_session.flush()
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert calls == [] and await _grants(db_session, u.id) == []
    identity = await db_session.get(PiGrantIdentity, u.id)
    await db_session.refresh(identity)
    assert identity.status == "none_confirmed" and identity.none_confirmed is True


async def test_an_orcid_id_name_is_no_match_without_reporter_calls(db_session, monkeypatch):
    u, job, calls = await _setup(db_session, monkeypatch, name="0000-0002-1825-009X")
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert calls == [] and (await db_session.get(PiGrantIdentity, u.id)).status == "no_match"


async def test_strict_orcid_failure_fails_the_job_before_any_write(db_session, monkeypatch):
    u, job, _ = await _setup(db_session, monkeypatch, fundings=httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        await ge.execute_enrich_grants(_ctx(job), db_session)
    assert await db_session.get(PiGrantIdentity, u.id) is None


async def test_fundings_are_stored_and_orcid_fetch_is_stamped(db_session, monkeypatch):
    f = OrcidFunding("hash:x", "Foundation award", "Golden Foundation", "grant", 2022, None,
                     2028, None, ())
    u, job, _ = await _setup(db_session, monkeypatch, fundings=[f])
    await ge.execute_enrich_grants(_ctx(job), db_session)
    titles = (await db_session.execute(select(PiOrcidFunding.title).where(
        PiOrcidFunding.user_id == u.id))).scalars().all()
    assert titles == ["Foundation award"]
    assert (await db_session.get(PiGrantIdentity, u.id)).orcid_fetched_at is not None


async def test_the_persona_is_rewritten_after_commit_only_when_it_changed(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    monkeypatch.setattr(pi_companies, "COMPANIES_DIR", tmp_path / "companies")
    linked = _row("R01AI000001", 2020, [_pi(111)])
    u, job, _ = await _setup(db_session, monkeypatch, stage1=[linked], stage2=[linked],
                             links={"R01AI000001": {"34187885"}})
    agent = await factories.make_agent(db_session, user=u, agent_id="grantlab", status="inactive")
    await factories.make_profile(db_session, user=u, research_summary="Studies TB.")
    ctx = _ctx(job)
    await ge.execute_enrich_grants(ctx, db_session)
    assert len(ctx.after_commit) == 1 and not (tmp_path / "public" / "grantlab.md").exists()
    await db_session.commit()
    await ctx.after_commit[0](db_session)
    persona = (tmp_path / "public" / "grantlab.md").read_text()
    assert "## Active Grants" in persona and "- T R01AI000001 (NIH R01, 2020–2099)" in persona
    job.status = "completed"
    job2 = Job(type="enrich_grants", user_id=u.id, payload=dict(job.payload))
    db_session.add(job2)
    await db_session.flush()
    ctx2 = _ctx(job2)
    await ge.execute_enrich_grants(ctx2, db_session)
    assert ctx2.after_commit == []
    revisions = (await db_session.execute(select(ProfileRevision.mechanism).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert revisions == ["pipeline"]


async def test_no_match_deletes_stale_rows_and_keeps_vetoed(db_session, monkeypatch):
    """Before, a run that found no PMID-linked candidate returned early and left every
    stored row rendering (E-3 c/d)."""
    two_unlinked = [_row("R01AA000001", 2020, [_pi(111)]), _row("R01BB000002", 2020, [_pi(112)])]
    u, job, _ = await _setup(db_session, monkeypatch, stage1=two_unlinked)
    db_session.add_all(_stale_and_vetoed(u.id))
    await db_session.flush()
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert [g.core_project_num for g in await _grants(db_session, u.id)] == ["R01VET00001"]
    assert (await db_session.get(PiGrantIdentity, u.id)).status == "no_match"


async def test_stage_two_uses_the_provisional_tenure_year(db_session, monkeypatch):
    """G-18: before, the job read get_tenure_start and ignored a provisional year."""
    linked = _row("R01AI000001", 2020, [_pi(111)])
    u, job, calls = await _setup(db_session, monkeypatch, stage1=[linked], stage2=[linked],
                                 links={"R01AI000001": {"34187885"}})
    await set_provisional_tenure_start(db_session, u.id, 2016)
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert calls[-1]["fiscal_years"][0] == 2016


def _record(pid, core="R01AI000001"):
    return GrantRecord(core, pid, f"T {core}", None, None, "R01", "AI", "Non-SBIR/STTR",
                       "JOHNS HOPKINS UNIVERSITY", 2020, 2024, None, None, 10, True, False)


async def _identity_cols(db_session, uid):
    return (await db_session.execute(text(
        "SELECT status, pinned_profile_ids FROM pi_grant_identity WHERE user_id = :u"),
        {"u": uid})).one()


async def test_a_pin_committed_during_the_run_wins_and_its_rerun_writes(db_session):
    """store_grant_outcome reads the staff columns at write time, not the run's view: an
    unpinned run's records are not stored under a pin, and the pin's status stands."""
    u = await factories.make_user(db_session)
    db_session.add(PiGrantIdentity(user_id=u.id, status="unconfirmed"))
    await db_session.flush()
    await db_session.execute(text("UPDATE pi_grant_identity SET pinned_profile_ids = ARRAY[222], "
                                  "status = 'pinned' WHERE user_id = :u"), {"u": u.id})
    outcome = ge.GrantOutcome("resolved", (111,), (), (111,), (_record(111),), "org_only", {},
                              True, "resolved")
    await ge.store_grant_outcome(db_session, u.id, outcome, now=datetime.now(UTC))
    assert await _grants(db_session, u.id) == []
    assert tuple(await _identity_cols(db_session, u.id)) == ("pinned", [222])


async def test_a_no_profile_mark_committed_during_the_run_clears_the_awards(db_session):
    """Before, the run's `resolved` records were inserted and only the status switched to
    none_confirmed, leaving rendering-eligible rows under that status."""
    u = await factories.make_user(db_session)
    db_session.add(PiGrantIdentity(user_id=u.id, status="unconfirmed"))
    await db_session.flush()
    await db_session.execute(text("UPDATE pi_grant_identity SET none_confirmed = true, "
                                  "status = 'none_confirmed' WHERE user_id = :u"), {"u": u.id})
    outcome = ge.GrantOutcome("resolved", (111,), (), (111,), (_record(111),), "org_only", {},
                              True, "resolved")
    await ge.store_grant_outcome(db_session, u.id, outcome, now=datetime.now(UTC))
    assert await _grants(db_session, u.id) == []
    assert (await _identity_cols(db_session, u.id)).status == "none_confirmed"


async def test_an_unpin_during_a_pinned_run_leaves_the_identity_unevaluated(db_session):
    """Before, the pinned run wrote status 'pinned' with pinned_profile_ids NULL (the
    manager page then broke) and stored the pinned awards."""
    u = await factories.make_user(db_session)
    db_session.add(PiGrantIdentity(user_id=u.id, status="pinned", pinned_profile_ids=[111],
                                   pinned_at=datetime.now(UTC)))
    await db_session.flush()
    await db_session.execute(text("UPDATE pi_grant_identity SET pinned_profile_ids = NULL, "
                                  "status = NULL WHERE user_id = :u"), {"u": u.id})
    outcome = ge.GrantOutcome("pinned", (), (), (111,), (_record(111),), "org_only", {},
                              False, "pinned")
    await ge.store_grant_outcome(db_session, u.id, outcome, now=datetime.now(UTC))
    assert await _grants(db_session, u.id) == []
    assert tuple(await _identity_cols(db_session, u.id)) == (None, None)


async def test_a_pinned_stage_two_firehose_keeps_the_stored_awards(db_session, monkeypatch):
    """Before, a pinned PI's over-cap stage 2 deleted every non-vetoed row while the
    status stayed `pinned`."""
    u, job, _ = await _setup(db_session, monkeypatch, stage2=ReporterFirehoseError("x"))
    identity = PiGrantIdentity(user_id=u.id, status="pinned", pinned_profile_ids=[111],
                               pinned_at=datetime.now(UTC))
    db_session.add(identity)
    db_session.add(PiGrant(user_id=u.id, core_project_num="R01KEPT0001", title="kept",
                           org_name="X", tenure_filter_mode="org_only", reporter_profile_id=111))
    await db_session.flush()
    await ge.execute_enrich_grants(_ctx(job), db_session)
    assert [g.core_project_num for g in await _grants(db_session, u.id)] == ["R01KEPT0001"]
    assert (await _identity_cols(db_session, u.id)).status == "pinned"
    outcome = await ge.resolve_grants(db_session, u, identity)
    assert outcome.stage2_firehose and "firehose" in outcome.note


async def test_enqueue_skips_an_active_type_and_carries_not_before(db_session):
    u = await factories.make_user(db_session)
    db_session.add(Job(type="enrich_grants", user_id=u.id, status="pending", payload={}))
    await db_session.flush()
    when = datetime(2030, 1, 1, tzinfo=UTC)
    await ge.enqueue_enrichment_jobs(db_session, u.id, u.orcid, not_before=when)
    jobs = (await db_session.execute(select(Job).where(Job.user_id == u.id))).scalars().all()
    assert len([j for j in jobs if j.type == "enrich_grants"]) == 1
    [industry] = [j for j in jobs if j.type == "industry_evidence"]
    assert industry.not_before == when
