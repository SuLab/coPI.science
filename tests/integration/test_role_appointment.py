"""Admin-UI role appointment, and the guards that keep it from locking
everyone out of /admin."""

import uuid

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


async def test_a_pi_cannot_appoint_anyone(client, db_session):
    """The manager case (test_a_manager_cannot_appoint_anyone) and this one are
    both worth pinning explicitly on a privilege endpoint — a PI is gated out
    by the same get_admin_user dependency, but that shouldn't be inferred from
    the manager case alone."""
    pi_actor = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    target = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{target.id}/role",
        data={"user_role": USER_ROLE_ADMIN},
        headers=auth_headers(pi_actor.id),
    )
    assert r.status_code == 403
    assert await _role_of(db_session, target.id) == USER_ROLE_PI


async def test_a_nonexistent_target_user_gets_404(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{uuid.uuid4()}/role",
        data={"user_role": USER_ROLE_MANAGER},
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 404


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


@pytest.mark.parametrize(
    "other_admin_access,expect_refused",
    [("denied", True), ("allowed", False)],
    ids=["other-admin-cannot-log-in", "other-admin-can-log-in"],
)
async def test_the_last_admin_guard_counts_only_admins_who_can_log_in(
    db_session, other_admin_access, expect_refused
):
    """The counterexample that an unfiltered count gets wrong.

    Admins X (access_status='denied') and Y ('allowed'). Counting every row
    with user_role='admin' gives 2, so `<= 1` does not fire and Y is
    demotable — leaving zero admins who can actually reach /admin, because
    auth.py refuses a session to anyone whose access_status is not 'allowed'.
    A larger count makes the guard fire LESS often, so the unfiltered version
    was not "more conservative"; it was weaker.

    The parametrized second case is the false-pass guard: flip X to 'allowed'
    and the identical call must go through. Without it, a guard hard-wired to
    refuse every demotion would pass the first case.
    """
    from fastapi import HTTPException

    from src.routers.admin import admin_set_user_role

    await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, access_status=other_admin_access
    )
    target = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, access_status="allowed"
    )
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    async def _demote():
        return await admin_set_user_role(
            user_id=target.id,
            request=None,          # the handler never reads it
            user_role=USER_ROLE_PI,
            db=db_session,
            current_user=actor,
        )

    if expect_refused:
        with pytest.raises(HTTPException) as exc:
            await _demote()
        assert exc.value.status_code == 400
        assert "last remaining admin" in exc.value.detail
        assert await _role_of(db_session, target.id) == USER_ROLE_ADMIN
    else:
        await _demote()
        assert await _role_of(db_session, target.id) == USER_ROLE_PI


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
