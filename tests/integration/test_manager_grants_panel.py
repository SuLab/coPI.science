"""Manager grants card and its write routes (spec 2026-10-05 §6.1)."""
import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    Job,
    PiGrant,
    PiGrantIdentity,
    PiIndustryEvidence,
    PiOrcidFunding,
    ProfileRevision,
    ResearcherProfile,
)
from src.models.job import INTERACTIVE_PRIORITY
from src.routers import manager as manager_routes
from src.services import profile_export
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _grant(user_id, core, title, pid=111, **kw):
    return PiGrant(
        user_id=user_id, core_project_num=core, title=title, reporter_profile_id=pid,
        activity_code="R01", org_name="JOHNS HOPKINS UNIVERSITY", first_fy=2020, last_fy=2026,
        project_end=datetime(2099, 6, 30, tzinfo=UTC), tenure_filter_mode="org_only", **kw,
    )


async def _pi(db_session, tmp_path, monkeypatch, *, status="resolved", with_agent=True):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    pi = await factories.make_user(db_session)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_profile(db_session, user=pi, profile_version=4)
    agent = await factories.make_agent(db_session, user=pi) if with_agent else None
    db_session.add(PiGrantIdentity(user_id=pi.id, status=status, accepted_profile_ids=[111], candidates=[
        {"id": 111, "name_on_award": "Fidel Zavala", "linked": True, "linking_pmids": ["9"]},
        {"id": 222, "name_on_award": "F. Zavala", "linked": False, "linking_pmids": []}]))
    await db_session.flush()
    return pi, mgr, agent


async def test_card_shows_status_candidates_evidence_and_the_d57_note(
    client, db_session, tmp_path, monkeypatch
):
    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch, status="unconfirmed")
    db_session.add(_grant(pi.id, "R01AI137329", "Beta-lactam resistance", identity_evidence={
        "matched_profile_id": 111, "name_on_award": "Fidel Zavala",
        "linking_pmids": ["34187885"], "rule": "pmid_link"}))
    db_session.add(PiOrcidFunding(
        user_id=pi.id, group_key="k", title="Foundation award", funder_name="Golden Foundation",
    ))
    await db_session.commit()
    html = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    for expected in (
        "unconfirmed", "F. Zavala", "RePORTER profile 222", "This is the PI",
        "PI has no RePORTER profile", "Beta-lactam resistance", "PMID 34187885",
        "Foundation award", "set a tenure year to show past grants",
    ):
        assert expected in html, expected


async def test_the_d57_note_follows_the_export_tenure_and_a_cut_name_is_flagged(
    client, db_session, tmp_path, monkeypatch
):
    from src.services.jhu_rules import set_provisional_tenure_start

    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch)
    await set_provisional_tenure_start(db_session, pi.id, 2015)
    pi.name_sanitized_at = datetime.now(UTC)
    await db_session.commit()
    html = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "set a tenure year to show past grants" not in html, "a provisional year counts (D57)"
    assert "check the spelling" in html


async def test_grant_veto_records_actor_revision_and_rewrites_the_persona(
    client, db_session, tmp_path, monkeypatch
):
    pi, mgr, agent = await _pi(db_session, tmp_path, monkeypatch)
    wrong = _grant(pi.id, "R01XX000001", "Wrong person grant")
    db_session.add_all([wrong, _grant(pi.id, "R01XX000002", "Right grant")])
    await db_session.commit()
    r = await client.post(f"/manager/pis/{pi.id}/grants/{wrong.id}/veto",
                          headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    await db_session.refresh(wrong)
    assert wrong.vetoed_at is not None and wrong.vetoed_by_user_id == mgr.id
    text_out = (tmp_path / "public" / f"{agent.agent_id}.md").read_text()
    assert "Right grant (NIH R01, 2020–2099)" in text_out and "Wrong person grant" not in text_out
    rev = (await db_session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
    )).scalar_one()
    assert (rev.mechanism, rev.content, rev.changed_by_user_id) == ("grant_veto", text_out, mgr.id)
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.profile_version == 4


async def test_a_second_veto_keeps_the_first_and_other_pis_grants_are_404(
    client, db_session, tmp_path, monkeypatch
):
    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch)
    other = await factories.make_user(db_session)
    g = _grant(pi.id, "R01XX000012", "Twice vetoed")
    foreign = _grant(other.id, "R01XX000013", "Someone else's")
    db_session.add_all([g, foreign])
    await db_session.commit()
    url = f"/manager/pis/{pi.id}/grants/{g.id}/veto"
    await client.post(url, headers=auth_headers(mgr.id), follow_redirects=False)
    await db_session.refresh(g)
    first = g.vetoed_at
    await client.post(url, headers=auth_headers(mgr.id), follow_redirects=False)
    await db_session.refresh(g)
    assert first is not None and g.vetoed_at == first
    r = await client.post(f"/manager/pis/{pi.id}/grants/{foreign.id}/veto",
                          headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 404


async def test_veto_under_impersonation_logs_the_real_admin(
    client, db_session, tmp_path, monkeypatch, caplog
):
    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    g = _grant(pi.id, "R01XX000020", "Impersonated veto")
    db_session.add(g)
    await db_session.commit()
    with caplog.at_level(logging.WARNING, logger="src.routers.manager"):
        await client.post(f"/manager/pis/{pi.id}/grants/{g.id}/veto",
                          headers=auth_headers(admin.id, impersonate=mgr.id),
                          follow_redirects=False)
    assert "recorded under impersonated user" in caplog.text
    await db_session.refresh(g)
    assert g.vetoed_by_user_id == mgr.id


async def test_pin_two_candidates_pins_and_requests_enrich_grants(
    client, db_session, tmp_path, monkeypatch
):
    pi, mgr, agent = await _pi(db_session, tmp_path, monkeypatch, status="held")
    await db_session.commit()
    r = await client.post(f"/manager/pis/{pi.id}/grant-identity/pin",
                          data={"profile_ids": ["111", "222", "999"]},
                          headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    identity = await db_session.get(PiGrantIdentity, pi.id)
    await db_session.refresh(identity)
    assert (identity.status, identity.pinned_profile_ids, identity.pinned_by_user_id) == (
        "pinned", [111, 222], mgr.id)
    job = (await db_session.execute(
        select(Job).where(Job.user_id == pi.id, Job.type == "enrich_grants")
    )).scalar_one()
    assert job.status == "pending" and job.priority == INTERACTIVE_PRIORITY
    mechanisms = (await db_session.execute(select(ProfileRevision.mechanism).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert mechanisms == ["grant_pin"]


async def test_pin_while_enrich_grants_runs_requests_a_rerun(
    client, db_session, tmp_path, monkeypatch
):
    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch, status="unconfirmed")
    running = Job(type="enrich_grants", user_id=pi.id, payload={}, status="processing",
                  attempts=1)
    db_session.add(running)
    await db_session.commit()
    await client.post(f"/manager/pis/{pi.id}/grant-identity/pin", data={"profile_ids": ["222"]},
                      headers=auth_headers(mgr.id), follow_redirects=False)
    await db_session.refresh(running)
    assert running.rerun_requested_at is not None
    assert len((await db_session.execute(select(Job).where(Job.user_id == pi.id))).scalars().all()) == 1


async def test_a_typed_id_needs_one_reporter_search(client, db_session, tmp_path, monkeypatch):
    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch, status="no_match")
    await db_session.commit()
    seen = []

    async def fake_exists(pid):
        seen.append(pid)
        return pid == 555

    monkeypatch.setattr(manager_routes, "profile_id_exists", fake_exists)
    bad = await client.post(f"/manager/pis/{pi.id}/grant-identity/pin",
                            data={"profile_id_text": "444"},
                            headers=auth_headers(mgr.id), follow_redirects=False)
    assert "error=invalid_reporter_id" in bad.headers["location"]
    await client.post(f"/manager/pis/{pi.id}/grant-identity/pin", data={"profile_id_text": "555"},
                      headers=auth_headers(mgr.id), follow_redirects=False)
    identity = await db_session.get(PiGrantIdentity, pi.id)
    await db_session.refresh(identity)
    assert seen == [444, 555] and identity.pinned_profile_ids == [555]


async def test_none_confirmed_then_unpin(client, db_session, tmp_path, monkeypatch):
    pi, mgr, _ = await _pi(db_session, tmp_path, monkeypatch, status="no_match")
    await db_session.commit()
    await client.post(f"/manager/pis/{pi.id}/grant-identity/none", headers=auth_headers(mgr.id),
                      follow_redirects=False)
    identity = await db_session.get(PiGrantIdentity, pi.id)
    await db_session.refresh(identity)
    assert (identity.status, identity.none_confirmed, identity.pinned_by_user_id) == (
        "none_confirmed", True, mgr.id)
    await client.post(f"/manager/pis/{pi.id}/grant-identity/unpin", headers=auth_headers(mgr.id),
                      follow_redirects=False)
    await db_session.refresh(identity)
    assert (
        identity.status, identity.none_confirmed, identity.pinned_profile_ids,
        identity.pinned_by_user_id,
    ) == (None, False, None, None)


async def test_orcid_veto_records_the_actor_and_drops_the_line(
    client, db_session, tmp_path, monkeypatch
):
    pi, mgr, agent = await _pi(db_session, tmp_path, monkeypatch)
    f = PiOrcidFunding(user_id=pi.id, group_key="k", title="Not mine",
                       funder_name="Golden Foundation", start_year=2024, end_year=2099)
    db_session.add(f)
    await db_session.commit()
    r = await client.post(f"/manager/pis/{pi.id}/orcid-fundings/{f.id}/veto",
                          headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    await db_session.refresh(f)
    assert f.vetoed_by_user_id == mgr.id
    assert "Not mine" not in (tmp_path / "public" / f"{agent.agent_id}.md").read_text()


async def test_non_pi_targets_are_404_on_every_new_route(client, db_session):
    staff = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    f = PiOrcidFunding(user_id=staff.id, group_key="k", title="T")
    db_session.add(f)
    await db_session.commit()
    for path in ("grant-identity/pin", "grant-identity/unpin", "grant-identity/none",
                 f"orcid-fundings/{f.id}/veto"):
        r = await client.post(f"/manager/pis/{staff.id}/{path}", headers=auth_headers(mgr.id),
                              follow_redirects=False)
        assert r.status_code == 404, path


async def test_grant_and_industry_vetoes_on_a_non_pi_account_are_404(client, db_session):
    staff = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    g = _grant(staff.id, "R01XX000015", "Staff grant")
    db_session.add(g)
    e = PiIndustryEvidence(
        user_id=staff.id, source="openalex", kind="coauthor_company", external_id="W9:I9",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    await db_session.commit()
    r1 = await client.post(f"/manager/pis/{staff.id}/grants/{g.id}/veto",
                           headers=auth_headers(mgr.id), follow_redirects=False)
    r2 = await client.post(f"/manager/pis/{staff.id}/industry/{e.id}/veto",
                           headers=auth_headers(mgr.id), follow_redirects=False)
    assert r1.status_code == 404 and r2.status_code == 404
    await db_session.refresh(g)
    await db_session.refresh(e)
    assert g.vetoed_at is None and e.vetoed_at is None


async def test_orcid_veto_refuses_on_a_lock_timeout(engine, tmp_path, monkeypatch):
    """A refresh holding the funding row: the veto gives up after lock_timeout instead of
    waiting behind it. Committing sessions: the holder must be another connection."""
    monkeypatch.setattr(manager_routes, "ORCID_VETO_LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        pi = await factories.make_user(s)
        mgr = await factories.make_user(s, user_role=USER_ROLE_MANAGER)
        f = PiOrcidFunding(user_id=pi.id, group_key="k", title="Busy")
        s.add(f)
        await s.commit()
    try:
        async with factory() as holder:
            await holder.execute(
                text("SELECT 1 FROM pi_orcid_fundings WHERE id = :i FOR UPDATE"), {"i": f.id}
            )
            async with factory() as s2:
                resp = await asyncio.wait_for(manager_routes.manager_veto_orcid_funding(
                    pi.id, f.id, request=SimpleNamespace(session={}), db=s2, current_user=mgr,
                ), timeout=10)
            assert "error=orcid_busy" in resp.headers["location"]
            await holder.rollback()
        async with factory() as s:
            vetoed_at = (await s.execute(
                select(PiOrcidFunding.vetoed_at).where(PiOrcidFunding.id == f.id)
            )).scalar_one()
            assert vetoed_at is None
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id IN (:a, :b)"),
                            {"a": pi.id, "b": mgr.id})
            await s.commit()
