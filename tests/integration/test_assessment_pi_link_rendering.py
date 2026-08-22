"""The Lab cell on both assessment surfaces links to the PI's profile when
resolvable, and falls back to plain text otherwise (design D9/D10)."""
import pytest

from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _run_and_assessment(db_session, subject_agent_id):
    from src.models import OpportunityAssessment
    run = await factories.make_simulation_run(db_session)
    a = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id=subject_agent_id, channel_name="general",
    )
    db_session.add(a)
    await db_session.flush()
    return run, a


async def test_admin_assessments_list_links_to_admin_users_page(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wang")
    run, _ = await _run_and_assessment(db_session, "wang")

    r = await client.get(f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id))
    assert f'/admin/users/{pi.id}' in r.text


async def test_manager_assessments_list_links_to_manager_pis_page(client, db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wu")
    run, _ = await _run_and_assessment(db_session, "wu")

    r = await client.get(f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id))
    assert f'/manager/pis/{pi.id}' in r.text


async def test_unresolvable_subject_renders_plain_text_no_link(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run, _ = await _run_and_assessment(db_session, "decommissioned-slug")

    r = await client.get(f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id))
    assert "decommissioned-slug" in r.text
    assert '/admin/users/' not in r.text.split("decommissioned-slug")[0][-200:]


async def test_admin_assessment_detail_links_to_pi_profile(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="su")
    _, a = await _run_and_assessment(db_session, "su")

    r = await client.get(f"/admin/assessments/{a.id}", headers=auth_headers(admin.id))
    assert f'/admin/users/{pi.id}' in r.text
