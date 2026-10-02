"""A-07: the two remaining doors out of a loginable admin — deny and an admin's
delete of another account — keep at least one allowed admin, and deny refuses the
caller's own account (including the real admin behind an impersonated session).
The last-admin branches are unreachable over HTTP without a race (the actor is an
allowed admin), so they are driven by calling the handler with a non-admin actor,
as tests/integration/test_role_appointment.py does."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from src.routers.admin.access import admin_deny_access
from src.routers.admin.users import admin_delete_user
from tests import factories
from tests.integration._webui_helpers import impersonation_headers
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _status(db, user_id):
    return (await db.execute(select(User.access_status).where(User.id == user_id))).scalar_one()


async def test_an_admin_cannot_deny_their_own_access(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/access-requests/{admin.id}/deny",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 400
    assert await _status(db_session, admin.id) == "allowed"


async def test_deny_under_impersonation_refuses_the_real_admins_own_account(client, db_session):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/access-requests/{real.id}/deny",
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    assert r.status_code == 400
    assert await _status(db_session, real.id) == "allowed"


async def test_deny_still_denies_a_pi(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, access_status="pending")
    r = await client.post(
        f"/admin/access-requests/{pi.id}/deny",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status(db_session, pi.id) == "denied"


async def test_deny_refuses_to_remove_the_last_allowed_admin(db_session):
    sole = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    with pytest.raises(HTTPException) as exc:
        await admin_deny_access(user_id=sole.id, request=None, db=db_session, current_user=actor)
    assert exc.value.status_code == 400
    assert "last remaining admin" in exc.value.detail
    assert await _status(db_session, sole.id) == "allowed"


async def test_denying_an_already_denied_admin_is_not_a_last_admin_refusal(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    denied_admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, access_status="denied"
    )
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await admin_deny_access(user_id=denied_admin.id, request=None, db=db_session, current_user=actor)
    assert await _status(db_session, denied_admin.id) == "denied"


async def test_delete_refuses_to_remove_the_last_allowed_admin(db_session):
    sole = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    with pytest.raises(HTTPException) as exc:
        await admin_delete_user(
            user_id=sole.id, request=None, remove_from_allowlist="",
            db=db_session, current_user=actor,
        )
    assert exc.value.status_code == 400
    assert "last remaining admin" in exc.value.detail
    assert (await db_session.execute(select(User.id).where(User.id == sole.id))).scalar_one_or_none()


async def test_one_admin_can_still_delete_another(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    other = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{other.id}/delete", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert (await db_session.execute(select(User.id).where(User.id == other.id))).scalar_one_or_none() is None
