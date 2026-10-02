"""A-14 remainder: cohort and agent notices travel as session flashes, never as query text."""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, Cohort
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


@pytest.mark.parametrize("path", ["/admin/cohorts", "/admin/cohorts/topology"])
async def test_cohort_pages_ignore_a_notice_in_the_query(client, db_session, path):
    admin = await _admin(db_session)
    await db_session.commit()
    r = await client.get(f"{path}?notice=SPOOFED-NOTICE", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert "SPOOFED-NOTICE" not in r.text


async def test_agent_detail_ignores_role_and_spoke_errors_in_the_query(client, db_session):
    admin = await _admin(db_session)
    agent = await factories.make_agent(db_session)
    await db_session.commit()
    r = await client.get(
        f"/admin/agents/{agent.id}?role_error=SPOOFED-ROLE&spoke_error=SPOOFED-SPOKE",
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 200
    assert "SPOOFED-ROLE" not in r.text and "SPOOFED-SPOKE" not in r.text


async def test_deleting_a_cohort_flashes_instead_of_a_notice_param(client, db_session):
    admin = await _admin(db_session)
    cohort = Cohort(name="flashdelete", created_by=admin.id)
    db_session.add(cohort)
    await db_session.commit()
    r = await client.post(f"/admin/cohorts/{cohort.id}/delete", headers=auth_headers(admin.id),
                          follow_redirects=False)
    assert r.status_code == 302
    assert "notice=" not in r.headers["location"]
    assert {"text": "Deleted cohort flashdelete", "kind": "success"} in session_flashes(r)
    assert (await db_session.execute(select(Cohort).where(Cohort.id == cohort.id))).first() is None


async def test_an_unknown_role_flashes_an_error(client, db_session):
    admin = await _admin(db_session)
    agent = await factories.make_agent(db_session)
    await db_session.commit()
    r = await client.post(f"/admin/agents/{agent.id}/role", data={"role": "no-such-role"},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302
    assert "role_error" not in r.headers["location"]
    assert {"text": "Role not changed: unknown role", "kind": "error"} in session_flashes(r)
