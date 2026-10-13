"""Admins see and remove a user's Microsoft sign-in (spec 2026-10-13 §5.3, §5.7, S5)."""
import uuid

import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    AdminAuditEvent,
    User,
    UserMicrosoftIdentity,
)
from tests import factories
from tests.oidc_fakes import OID, TENANT, make_config
from tests.session_support import session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


async def _linked_user(db_session, role=USER_ROLE_MANAGER):
    user = await factories.make_user(db_session, user_role=role)
    db_session.add(UserMicrosoftIdentity(user_id=user.id, tenant_id=uuid.UUID(TENANT),
                                         object_id=uuid.UUID(OID), sign_in_name="staff@example.edu"))
    await db_session.flush()
    return user


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


async def test_an_admin_removes_a_link_and_ends_the_users_sessions(client, db_session, on):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    r = await client.post(f"/admin/users/{user.id}/microsoft/unlink", headers=session_headers(admin.id))
    assert r.status_code == 302 and r.headers["location"] == f"/admin/users/{user.id}"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch == 1
    event = await db_session.scalar(select(AdminAuditEvent).where(AdminAuditEvent.action == "microsoft_unlink"))
    assert event.actor_user_id == admin.id
    assert event.payload == {"user_id": str(user.id), "tenant_id": TENANT, "sign_in_name": "staff@example.edu"}


async def test_removing_a_missing_link_writes_no_audit_row(client, db_session, on):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.post(f"/admin/users/{user.id}/microsoft/unlink", headers=session_headers(admin.id))
    assert r.status_code == 302
    assert await db_session.scalar(select(func.count()).select_from(AdminAuditEvent)
                                   .where(AdminAuditEvent.action == "microsoft_unlink")) == 0


async def test_managers_impersonation_and_feature_off_are_refused(client, db_session, asgi_app):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    path = f"/admin/users/{user.id}/microsoft/unlink"
    assert (await client.post(path, headers=session_headers(admin.id))).status_code == 404
    asgi_app.state.microsoft_sign_in = make_config()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assert (await client.post(path, headers=session_headers(manager.id))).status_code == 403
    r = await client.post(path, headers=session_headers(admin.id, impersonate=manager.id))
    assert r.status_code == 403
    other_admin = await _admin(db_session)  # passes get_admin_user; refuse_impersonation stops it
    r = await client.post(path, headers=session_headers(admin.id, impersonate=other_admin.id))
    assert r.status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AdminAuditEvent)
                                   .where(AdminAuditEvent.action == "microsoft_unlink")) == 0


async def test_the_admin_user_page_shows_the_link(client, db_session, on):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    r = await client.get(f"/admin/users/{user.id}", headers=session_headers(admin.id))
    assert "staff@example.edu" in r.text and "Remove Microsoft sign-in" in r.text


async def test_a_demoted_pis_link_is_removable_from_the_workspace_page(client, db_session, on):
    admin, pi = await _admin(db_session), await _linked_user(db_session, USER_ROLE_PI)
    await factories.make_profile(db_session, user=pi)
    r = await client.get(f"/workspace/pis/{pi.id}", headers=session_headers(admin.id))
    assert r.status_code == 200 and "Remove Microsoft sign-in" in r.text
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get(f"/workspace/pis/{pi.id}", headers=session_headers(manager.id))
    assert "Remove Microsoft sign-in" not in r.text and "staff@example.edu" not in r.text
