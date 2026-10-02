"""Only an administrator (any user) or a manager (PIs) verifies an address, never under
impersonation, always with an audit event (spec 2026-10-01 §6.6, D7)."""
import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AdminAuditEvent,
)
from src.services.email_verification import VERIFY_EMAIL_ACTION
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _events(db_session):
    return (await db_session.execute(
        select(AdminAuditEvent).where(AdminAuditEvent.action == VERIFY_EMAIL_ACTION)
    )).scalars().all()


async def _user(db_session, role, **overrides):
    user = await factories.make_user(db_session, user_role=role, **overrides)
    await db_session.flush()
    return user


# --- admin route ----------------------------------------------------------------

async def test_an_admin_verifies_any_users_address(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_REVIEWER, email="rev@example.edu")
    r = await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(admin.id))
    assert r.status_code == 302
    assert r.headers["location"] == f"/admin/users/{target.id}?email_verified=1"
    await db_session.refresh(target)
    assert target.email_verified_at is not None
    assert [(e.actor_user_id, e.payload) for e in await _events(db_session)] == [
        (admin.id, {"user_id": str(target.id), "email": "rev@example.edu"})
    ]


async def test_the_admin_route_refuses_under_impersonation(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    other_admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/admin/users/{target.id}/verify-email",
                          headers=auth_headers(admin.id, impersonate=other_admin.id))
    assert r.status_code == 403
    await db_session.refresh(target)
    assert target.email_verified_at is None and await _events(db_session) == []


async def test_an_account_without_an_address_is_not_verified(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_PI, email=None)
    r = await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(admin.id))
    assert r.headers["location"] == f"/admin/users/{target.id}?error=no_email"
    await db_session.refresh(target)
    assert target.email_verified_at is None and await _events(db_session) == []


async def test_the_admin_route_is_admin_only(client, db_session):
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    target = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(mgr.id))
    assert r.status_code == 403
    admin = await _user(db_session, USER_ROLE_ADMIN)
    missing = await client.post(
        "/admin/users/00000000-0000-0000-0000-000000000000/verify-email",
        headers=auth_headers(admin.id),
    )
    assert missing.status_code == 404


# --- manager route --------------------------------------------------------------

async def test_a_manager_verifies_a_pis_address(client, db_session):
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    pi = await _user(db_session, USER_ROLE_PI, email="pi@example.edu")
    r = await client.post(f"/manager/pis/{pi.id}/verify-email", headers=auth_headers(mgr.id))
    assert r.headers["location"] == f"/manager/pis/{pi.id}?email_verified=1"
    await db_session.refresh(pi)
    assert pi.email_verified_at is not None
    assert [e.actor_user_id for e in await _events(db_session)] == [mgr.id]


@pytest.mark.parametrize("role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_a_manager_cannot_verify_a_non_pi(client, db_session, role):
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    target = await _user(db_session, role)
    r = await client.post(f"/manager/pis/{target.id}/verify-email", headers=auth_headers(mgr.id))
    assert r.status_code == 404
    await db_session.refresh(target)
    assert target.email_verified_at is None


@pytest.mark.parametrize("role", [USER_ROLE_PI, USER_ROLE_REVIEWER])
async def test_the_manager_route_refuses_non_staff(client, db_session, role):
    caller = await _user(db_session, role)
    pi = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/manager/pis/{pi.id}/verify-email", headers=auth_headers(caller.id))
    assert r.status_code == 403


async def test_the_manager_route_refuses_under_impersonation(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    pi = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/manager/pis/{pi.id}/verify-email",
                          headers=auth_headers(admin.id, impersonate=mgr.id))
    assert r.status_code == 403
    await db_session.refresh(pi)
    assert pi.email_verified_at is None and await _events(db_session) == []


# --- the controls ----------------------------------------------------------------

async def test_the_admin_page_offers_verification_until_it_is_done(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_PI, email="ctl@example.edu")
    action = f'action="/admin/users/{target.id}/verify-email"'
    page = (await client.get(f"/admin/users/{target.id}", headers=auth_headers(admin.id))).text
    assert action in page and "Unverified" in page
    await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(admin.id))
    page = (await client.get(f"/admin/users/{target.id}", headers=auth_headers(admin.id))).text
    assert action not in page and "Verified " in page


async def test_the_manager_page_offers_verification_only_to_staff_not_impersonating(
    client, db_session
):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    rev = await _user(db_session, USER_ROLE_REVIEWER)
    pi = await _user(db_session, USER_ROLE_PI, email="ctl2@example.edu")
    action = f'action="/manager/pis/{pi.id}/verify-email"'
    assert action in (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert action not in (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(rev.id))).text
    imp = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(admin.id, impersonate=mgr.id))
    assert imp.status_code == 200 and action not in imp.text
