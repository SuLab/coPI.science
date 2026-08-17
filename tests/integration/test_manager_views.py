"""The /manager surface: deny-by-default, read-only, and PI-scoped."""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.routers import manager as manager_router
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _manager_get_paths() -> list[str]:
    """Every GET path on the manager router, with path params filled by name.

    Enumerated from the router rather than hand-listed so a route added later
    is automatically covered by the sweeps below. This is what keeps
    deny-by-default honest instead of aspirational.
    """
    return sorted(
        r.path for r in manager_router.router.routes if "GET" in getattr(r, "methods", ())
    )


def test_manager_router_exposes_no_mutating_routes():
    """D12. If there is no mutation route there is no mutation risk, and this
    turns that from a promise into a check."""
    methods = {m for r in manager_router.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"GET"}, f"non-GET route on the manager router: {methods}"


async def test_unauthenticated_manager_root_redirects_to_login(client):
    r = await client.get("/manager", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


async def test_pi_is_denied_the_manager_surface(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    for path in ("/manager", "/manager/pis"):
        r = await client.get(path, headers=auth_headers(pi.id), follow_redirects=False)
        assert r.status_code == 403, f"{path} was reachable by a PI"


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_staff_can_read_the_pi_directory(client, db_session, role):
    staff = await factories.make_user(db_session, user_role=role)
    await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Dr Target")
    r = await client.get("/manager/pis", headers=auth_headers(staff.id))
    assert r.status_code == 200
    assert "Dr Target" in r.text


async def test_manager_root_redirects_to_the_pi_directory(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get("/manager", headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis"


async def test_directory_excludes_staff_accounts(client, db_session):
    """Ruling A: `templates/base.html` renders `{{ current_user.name }}`
    unconditionally in the nav, so the *viewing* manager's own name is always
    present in the page — asserting it is absent can never pass. Link-based
    assertions test the query (which staff rows are excluded from the
    directory) rather than the nav."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Self")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Sneaky Admin")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Real PI")
    r = await client.get("/manager/pis", headers=auth_headers(mgr.id))
    assert "Real PI" in r.text
    assert f"/manager/pis/{pi.id}" in r.text
    assert f"/manager/pis/{admin.id}" not in r.text
    assert "Sneaky Admin" not in r.text


async def test_pi_detail_is_readable(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, name="Dr Detail", email="pi@example.edu"
    )
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert "Dr Detail" in r.text
    assert "pi@example.edu" in r.text  # D3: contact info is in scope


async def test_pi_detail_404s_for_a_staff_account(client, db_session):
    """Closes UUID enumeration of admin/manager records."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(f"/manager/pis/{admin.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 404


async def test_pi_detail_has_no_delete_or_impersonate_control(client, db_session):
    """F6: the admin templates carry both; the manager templates must not."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "/delete" not in body
    assert "impersonate" not in body.lower()
    assert "Danger Zone" not in body


async def test_manager_is_denied_every_admin_route(client, db_session):
    from src.routers import admin as admin_router

    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    checked = 0
    for route in admin_router.router.routes:
        if "GET" not in getattr(route, "methods", ()) or "{" in route.path:
            continue
        r = await client.get(
            f"/admin{route.path}", headers=auth_headers(mgr.id), follow_redirects=False
        )
        assert r.status_code == 403, f"/admin{route.path} leaked to a manager"
        checked += 1
    assert checked >= 8, "the admin sweep matched too few routes to be meaningful"


async def test_manager_cannot_impersonate(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid}, headers=auth_headers(mgr.id)
    )
    assert r.status_code == 403


async def test_a_hand_set_impersonate_cookie_is_ignored_for_a_manager(client, db_session):
    """F7: the cookie is unsigned and client-supplied. get_current_user honours
    it only for is_admin, which a manager never satisfies."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="TheAdmin")
    headers = auth_headers(mgr.id)
    headers["Cookie"] += f"; copi-impersonate={admin.id}"
    r = await client.get("/manager/pis", headers=headers, follow_redirects=False)
    assert r.status_code == 200          # still the manager, not the admin
    r2 = await client.get("/admin/users", headers=headers, follow_redirects=False)
    assert r2.status_code == 403         # did NOT become an admin
