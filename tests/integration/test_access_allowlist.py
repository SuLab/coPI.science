"""SN-01: adding an ORCID to the allowlist promotes a PENDING request; it never
silently reverses an earlier deny (the Approve button on the denied row does that)."""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, AccessAllowlist, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _status(db, user_id):
    return (await db.execute(select(User.access_status).where(User.id == user_id))).scalar_one()


async def test_allowlisting_a_denied_orcid_does_not_reverse_the_deny(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    denied = await factories.make_user(db_session, access_status="denied")
    r = await client.post(
        "/admin/access-allowlist/add", data={"orcid": denied.orcid, "note": ""},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status(db_session, denied.id) == "denied"
    row = (await db_session.execute(
        select(AccessAllowlist).where(AccessAllowlist.orcid == denied.orcid)
    )).scalar_one()
    assert row.added_by_user_id == admin.id


async def test_allowlisting_a_pending_orcid_promotes_it(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pending = await factories.make_user(
        db_session, access_status="pending", onboarding_complete=False
    )
    r = await client.post(
        "/admin/access-allowlist/add", data={"orcid": pending.orcid, "note": ""},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status(db_session, pending.id) == "allowed"
