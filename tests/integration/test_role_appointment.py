"""Admin-UI role appointment, and the guards that keep it from locking
everyone out of /admin."""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _role_of(db_session, user_id) -> str:
    return await db_session.scalar(select(User.user_role).where(User.id == user_id))


async def test_admin_can_promote_a_pi_to_manager(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{pi.id}/role",
        data={"user_role": USER_ROLE_MANAGER},
        headers=auth_headers(admin.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, pi.id) == USER_ROLE_MANAGER


async def test_a_manager_cannot_appoint_anyone(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{pi.id}/role",
        data={"user_role": USER_ROLE_ADMIN},
        headers=auth_headers(mgr.id),
    )
    assert r.status_code == 403
    assert await _role_of(db_session, pi.id) == USER_ROLE_PI


async def test_an_invalid_role_is_rejected(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{pi.id}/role",
        data={"user_role": "superuser"},
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 400
    assert await _role_of(db_session, pi.id) == USER_ROLE_PI


async def test_an_admin_cannot_change_their_own_role(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{admin.id}/role",
        data={"user_role": USER_ROLE_PI},
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 400
    assert await _role_of(db_session, admin.id) == USER_ROLE_ADMIN


async def test_an_admin_can_demote_another_admin_when_two_exist(client, db_session):
    a1 = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    a2 = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{a2.id}/role",
        data={"user_role": USER_ROLE_PI},
        headers=auth_headers(a1.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, a2.id) == USER_ROLE_PI


async def test_the_last_admin_guard_fires_when_the_target_is_the_only_admin(db_session):
    """Defense in depth, exercised by calling the handler directly.

    It is UNREACHABLE over HTTP while the self-change guard stands, and that is
    not an accident: demoting the last admin X requires an actor with admin
    rights who is not X, and if X is the last admin no such actor exists. The
    self-change guard is therefore what actually prevents lockout today.

    Do not delete this guard as dead code, and do not "fix" this test into an
    HTTP one — it is the invariant's backstop if the self-change guard is ever
    relaxed, and the CLI (`role:set`, which has no guards by design) is the
    recovery path if lockout happens anyway.
    """
    from fastapi import HTTPException

    from src.routers.admin import admin_set_user_role

    sole_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    with pytest.raises(HTTPException) as exc:
        await admin_set_user_role(
            user_id=sole_admin.id,
            request=None,          # the handler never reads it
            user_role=USER_ROLE_PI,
            db=db_session,
            current_user=actor,
        )
    assert exc.value.status_code == 400
    assert "last remaining admin" in exc.value.detail
    assert await _role_of(db_session, sole_admin.id) == USER_ROLE_ADMIN


async def test_user_detail_shows_the_role_and_no_admin_yes_no_row(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (
        await client.get(f"/admin/users/{mgr.id}", headers=auth_headers(admin.id))
    ).text
    # Discriminating on the Role row's actual markup, not a bare substring —
    # the Account Type help text below names all three roles regardless of
    # whether this row renders correctly, so "manager" in body alone proves
    # nothing.
    assert '<dd class="font-medium">manager</dd>' in body
    # And the row this replaces must be genuinely gone, not just relabeled.
    assert ">Admin</dt>" not in body
