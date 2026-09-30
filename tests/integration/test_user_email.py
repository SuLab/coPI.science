"""A squatted or case-variant address never fails a login or lands a duplicate,
and the pending-access page never writes users.email."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, User
from src.routers import auth as auth_module
from src.services.user_email import assign_user_email
from tests import factories
from tests.integration.test_auth_allowlist_gate import _fake_oauth, _session_cookie
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _login(client, monkeypatch, orcid, email):
    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _fake_oauth(orcid))

    async def profile(orcid_id):
        return {"orcid": orcid_id, "name": "Login User", "email": email}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", profile)
    return await client.get(
        "/auth/callback?code=c&state=s", headers=_session_cookie({"oauth_state": "s"}),
    )


@pytest.mark.parametrize("variant", ["held@example.edu", "HELD@Example.edu"])
async def test_a_squatted_address_cannot_fail_a_login(client, db_session, monkeypatch, variant):
    await factories.make_user(db_session, email="held@example.edu")
    resp = await _login(client, monkeypatch, "0000-0002-1111-0001", variant)
    assert resp.status_code == 302, "a held address must not 500 the login"
    new = (await db_session.execute(
        select(User).where(User.orcid == "0000-0002-1111-0001")
    )).scalar_one()
    assert new.email is None


async def test_a_login_stores_orcids_address_exactly_as_given(client, db_session, monkeypatch):
    await _login(client, monkeypatch, "0000-0002-1111-0002", "Mixed.Case@Example.edu")
    new = (await db_session.execute(
        select(User).where(User.orcid == "0000-0002-1111-0002")
    )).scalar_one()
    assert new.email == "Mixed.Case@Example.edu", "no normalisation change"


async def test_a_case_variant_counts_as_taken_on_profile_save_and_onboarding(client, db_session):
    await factories.make_user(db_session, email="Taken@Example.edu")
    pi = await factories.make_user(db_session, email="mine@example.edu")
    resp = await client.post("/profile/save", data={
        "name": pi.name, "email": "taken@example.edu", "institution": "", "department": "",
        "research_summary": "", "techniques": "", "experimental_models": "",
        "disease_areas": "", "key_targets": "", "keywords": "",
    }, headers=auth_headers(pi.id))
    assert resp.headers["location"] == "/profile/edit?error=email_taken"
    resp = await client.post(
        "/onboarding/save-profile", data={"email": "TAKEN@example.edu"},
        headers=auth_headers(pi.id),
    )
    assert resp.headers["location"] == "/onboarding?error=email_taken"
    await db_session.refresh(pi)
    assert pi.email == "mine@example.edu"


async def test_the_pending_route_never_touches_users_email(client, db_session):
    pending = await factories.make_user(db_session, email=None, access_status="pending")
    resp = await client.post(
        "/access-pending/email", data={"email": "Someone@Else.edu"},
        headers=_session_cookie({"pending_access": {"user_id": str(pending.id)}}),
    )
    assert resp.status_code == 200
    await db_session.refresh(pending)
    assert pending.email is None
    assert pending.contact_email_unverified == "someone@else.edu"
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    page = await client.get("/admin/access-requests", headers=auth_headers(admin.id))
    assert "someone@else.edu" in page.text and "unverified" in page.text


async def test_an_integrity_race_is_confined_to_the_savepoint(db_session, monkeypatch):
    """Another session commits the exact address between the case-insensitive
    check and the flush."""
    await factories.make_user(db_session, email="race@example.edu")
    user = await factories.make_user(db_session, email=None)
    real_scalar = db_session.scalar
    state = {"first": True}

    async def check_misses_the_holder(*a, **kw):
        if state["first"]:
            state["first"] = False
            return None  # the holder "committed" after this check
        return await real_scalar(*a, **kw)

    monkeypatch.setattr(db_session, "scalar", check_misses_the_holder)
    assert await assign_user_email(db_session, user, "race@example.edu") is False
    assert user.email is None, "readable after the savepoint rollback"
    await db_session.flush()  # the outer transaction is still usable
    assert await assign_user_email(db_session, user, "free@example.edu") is True
    assert user.email == "free@example.edu"
