"""Impersonation lives in the signed session (spec 2026-10-01 §6.7, A-05); the unsigned
`copi-impersonate` cookie is neither read nor written."""
import time

import pytest

from src.dependencies import IMPERSONATE_EXPIRES_KEY, IMPERSONATE_KEY, IMPERSONATION_MAX_AGE
from src.models import USER_ROLE_ADMIN, USER_ROLE_PI
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.session_support import session_cookie_name, session_from_response, session_headers

pytestmark = pytest.mark.integration

BANNER = "Viewing as Imp Target"


def _set_cookie_names(response) -> list[str]:
    return [
        v.split("=", 1)[0] for k, v in response.headers.multi_items()
        if k.lower() == "set-cookie"
    ]


async def _admin_and_pi(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Imp Admin")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Imp Target")
    await factories.make_profile(db_session, user=pi)
    await db_session.flush()
    return admin, pi


async def test_starting_impersonation_writes_the_session_and_no_cookie(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    before = int(time.time())
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid}, headers=auth_headers(admin.id)
    )
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert _set_cookie_names(r) == [session_cookie_name()]
    session = session_from_response(r)
    assert session["user_id"] == str(admin.id)
    assert session[IMPERSONATE_KEY] == str(pi.id)
    assert (before + IMPERSONATION_MAX_AGE <= session[IMPERSONATE_EXPIRES_KEY]
            <= int(time.time()) + IMPERSONATION_MAX_AGE)


async def test_an_impersonating_session_views_as_the_target(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.get("/profile", headers=auth_headers(admin.id, impersonate=pi.id))
    assert r.status_code == 200
    assert BANNER in r.text


async def test_stop_drops_the_impersonation_and_writes_no_other_cookie(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.post(
        "/admin/impersonate/stop", headers=auth_headers(admin.id, impersonate=pi.id)
    )
    assert r.status_code == 302 and r.headers["location"] == "/admin/users"
    assert _set_cookie_names(r) == [session_cookie_name()]
    session = session_from_response(r)
    assert session["user_id"] == str(admin.id)
    assert IMPERSONATE_KEY not in session and IMPERSONATE_EXPIRES_KEY not in session


async def test_the_legacy_cookie_is_inert(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={pi.id}"
    r = await client.get("/profile", headers=headers)
    assert BANNER not in r.text
    control = await client.get("/profile", headers=auth_headers(admin.id, impersonate=pi.id))
    assert BANNER in control.text, "the control never impersonated, so the negative is vacuous"


async def test_a_lapsed_impersonation_is_dropped(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    headers = session_headers(
        admin.id, **{IMPERSONATE_KEY: str(pi.id), IMPERSONATE_EXPIRES_KEY: int(time.time()) - 1}
    )
    r = await client.get("/profile", headers=headers)
    assert BANNER not in r.text
    session = session_from_response(r)
    assert session is not None and IMPERSONATE_KEY not in session
    assert IMPERSONATE_EXPIRES_KEY not in session


async def test_an_impersonation_without_an_expiry_is_not_honoured(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.get("/profile", headers=session_headers(admin.id, **{IMPERSONATE_KEY: str(pi.id)}))
    assert BANNER not in r.text


async def test_a_non_admin_session_cannot_impersonate(client, db_session):
    _admin, pi = await _admin_and_pi(db_session)
    other = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Not Admin")
    await factories.make_profile(db_session, user=other)
    await db_session.flush()
    r = await client.get("/profile", headers=auth_headers(other.id, impersonate=pi.id))
    assert r.status_code == 200
    assert BANNER not in r.text


async def test_logout_clears_the_impersonation_with_the_session(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.post("/logout", headers=auth_headers(admin.id, impersonate=pi.id))
    assert "copi-impersonate" not in _set_cookie_names(r)
    assert session_from_response(r) == {}
