"""Manager UI: NIH grants panel + per-grant 'not this PI' veto (Task 5 of
docs/plans/2026-09-11-pi-external-enrichment-implementation-plan.md)."""
from unittest.mock import patch

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    PiGrant,
    PiIndustryEvidence,
    ResearcherProfile,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_grants_panel_lists_rows_with_evidence_badge(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(PiGrant(
        user_id=pi.id, core_project_num="R01AI137329", title="Beta-lactam resistance",
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", first_fy=2019, last_fy=2024,
        tenure_filter_mode="org_and_year", identity_evidence={"pmid_linked": [9751245]},
    ))
    await db_session.commit()
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))
    html = r.text
    assert "Beta-lactam resistance" in html and "PMID-linked" in html and "R01AI137329" in html


async def test_veto_hides_grant_and_drops_it_from_grant_titles(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(user_id=pi.id, grant_titles=["Wrong person grant"]))
    g = PiGrant(
        user_id=pi.id, core_project_num="R01XX000001", title="Wrong person grant",
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", tenure_filter_mode="org_only",
    )
    db_session.add(g)
    await db_session.commit()

    r = await client.post(
        f"/manager/pis/{pi.id}/grants/{g.id}/veto", data={}, headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302

    await db_session.refresh(g)
    assert g.vetoed_at is not None
    prof = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    await db_session.refresh(prof)
    assert prof.grant_titles == []


async def test_veto_keeps_non_reporter_titles_and_drops_only_the_vetoed_one(client, db_session):
    """A veto must not wipe titles that never came from a PiGrant row at all
    (e.g. ORCID/publication-derived)."""
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(
        user_id=pi.id, grant_titles=["ORCID-only title", "Wrong person grant"],
    ))
    g = PiGrant(
        user_id=pi.id, core_project_num="R01XX000002", title="Wrong person grant",
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", tenure_filter_mode="org_only",
    )
    db_session.add(g)
    await db_session.commit()

    r = await client.post(
        f"/manager/pis/{pi.id}/grants/{g.id}/veto", data={}, headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302

    prof = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    await db_session.refresh(prof)
    assert prof.grant_titles == ["ORCID-only title"]


async def test_veto_grant_belonging_to_another_pi_is_404(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    other_pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    g = PiGrant(
        user_id=other_pi.id, core_project_num="R01XX000003", title="Someone else's grant",
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", tenure_filter_mode="org_only",
    )
    db_session.add(g)
    await db_session.commit()

    r = await client.post(
        f"/manager/pis/{pi.id}/grants/{g.id}/veto", data={}, headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 404

    await db_session.refresh(g)
    assert g.vetoed_at is None


async def test_veto_reexports_profile_markdown_when_pi_has_an_agent(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    agent = await factories.make_agent(db_session, user=pi)
    db_session.add(ResearcherProfile(user_id=pi.id, grant_titles=["Wrong person grant"]))
    g = PiGrant(
        user_id=pi.id, core_project_num="R01XX000004", title="Wrong person grant",
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", tenure_filter_mode="org_only",
    )
    db_session.add(g)
    await db_session.commit()

    with patch("src.services.profile_publish.export_profile_to_markdown") as mock_export:
        r = await client.post(
            f"/manager/pis/{pi.id}/grants/{g.id}/veto", data={}, headers=auth_headers(mgr.id),
            follow_redirects=False,
        )
    assert r.status_code == 302
    mock_export.assert_called_once()
    called_agent_id = mock_export.call_args.args[2]
    assert called_agent_id == agent.agent_id


async def test_veto_skips_reexport_when_pi_has_no_agent(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(user_id=pi.id, grant_titles=["Wrong person grant"]))
    g = PiGrant(
        user_id=pi.id, core_project_num="R01XX000005", title="Wrong person grant",
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", tenure_filter_mode="org_only",
    )
    db_session.add(g)
    await db_session.commit()

    with patch("src.services.profile_publish.export_profile_to_markdown") as mock_export:
        r = await client.post(
            f"/manager/pis/{pi.id}/grants/{g.id}/veto", data={}, headers=auth_headers(mgr.id),
            follow_redirects=False,
        )
    assert r.status_code == 302
    mock_export.assert_not_called()


def _grant(user_id, title, project):
    return PiGrant(
        user_id=user_id, core_project_num=project, title=title,
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", last_fy=2024,
        tenure_filter_mode="org_only",
    )


async def test_veto_removes_only_the_vetoed_title_while_other_grants_remain(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(
        user_id=pi.id, grant_titles=["ORCID-only title", "Wrong person grant", "Right grant"],
    ))
    wrong = _grant(pi.id, "Wrong person grant", "R01XX000010")
    db_session.add_all([wrong, _grant(pi.id, "Right grant", "R01XX000011")])
    await db_session.commit()
    r = await client.post(
        f"/manager/pis/{pi.id}/grants/{wrong.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    prof = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    await db_session.refresh(prof)
    assert prof.grant_titles == ["ORCID-only title", "Right grant"]


async def test_a_second_veto_keeps_the_first_vetoed_at(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    g = _grant(pi.id, "Twice vetoed", "R01XX000012")
    db_session.add(g)
    await db_session.commit()
    url = f"/manager/pis/{pi.id}/grants/{g.id}/veto"
    await client.post(url, headers=auth_headers(mgr.id), follow_redirects=False)
    await db_session.refresh(g)
    first = g.vetoed_at
    r = await client.post(url, headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    await db_session.refresh(g)
    assert first is not None and g.vetoed_at == first


async def test_veto_keeps_a_title_another_grant_still_backs(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(user_id=pi.id, grant_titles=["Shared title"]))
    one = _grant(pi.id, "Shared title", "R01XX000013")
    db_session.add_all([one, _grant(pi.id, "Shared title", "R01XX000014")])
    await db_session.commit()
    await client.post(
        f"/manager/pis/{pi.id}/grants/{one.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    prof = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    await db_session.refresh(prof)
    assert prof.grant_titles == ["Shared title"]


async def test_veto_on_a_non_pi_account_is_404(client, db_session):
    staff = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    g = _grant(staff.id, "Staff grant", "R01XX000015")
    db_session.add(g)
    e = PiIndustryEvidence(
        user_id=staff.id, source="openalex", kind="coauthor_company", external_id="W9:I9",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    await db_session.commit()
    r1 = await client.post(
        f"/manager/pis/{staff.id}/grants/{g.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    r2 = await client.post(
        f"/manager/pis/{staff.id}/industry/{e.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r1.status_code == 404 and r2.status_code == 404
    await db_session.refresh(g)
    await db_session.refresh(e)
    assert g.vetoed_at is None and e.vetoed_at is None
