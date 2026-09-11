"""Manager UI: NIH grants panel + per-grant 'not this PI' veto (Task 5)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, USER_ROLE_PI, PiGrant, ResearcherProfile
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
