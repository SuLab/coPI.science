"""The Staff page's HTTP surface (plan docs/plans/2026-10-13-staff-accounts-without-a-bot-plan.md
§5.2, §6): the two-step add, refusals that redirect to the bare path with a session refill,
the reviewer actions, the legacy alias, and a created account's first sign-in.

Setup is committed (a savepoint release inside the test's outer transaction) before each
request, because a refused route rolls the shared session back and would otherwise take
the setup with it. Ids are copied to locals for the same reason: the rollback expires the
test's objects.
"""
import html
import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AdminAuditEvent,
    AgentRegistry,
    Job,
    User,
)
from src.routers import auth as auth_module
from src.services import staff_accounts
from src.services.staff_accounts import REFUSAL_MESSAGES, STAFF_REFILL_SESSION_KEY
from src.web.flash import FLASH_SESSION_KEY
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration

_NEW = "0000-0002-7000-0001"
_HELD = "0000-0002-7000-0002"
_STAFF_PAGE = "/workspace/staff"


@pytest.fixture
def orcid_record(monkeypatch):
    """The public ORCID record every fetch returns; tests edit it to make a preview stale."""
    record = {
        "name": "Jane Doe",
        "institution": "Johns Hopkins University",
        "employments": [
            {"organization": "Kennedy Krieger Institute", "start_year": 2019, "current": True},
        ],
    }

    async def fetch(orcid):
        return {"orcid": orcid, **record}

    monkeypatch.setattr(staff_accounts, "fetch_orcid_profile", fetch)
    return record


async def _user(db, **overrides):
    user = await factories.make_user(db, **overrides)
    return user.id


async def _users(db) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def _audits(db) -> int:
    return (await db.execute(select(func.count()).select_from(AdminAuditEvent))).scalar_one()


async def _state(db, user_id):
    return (
        await db.execute(
            select(User.user_role, User.access_status, User.session_epoch).where(User.id == user_id)
        )
    ).one_or_none()


async def _id_for(db, orcid):
    return (await db.execute(select(User.id).where(User.orcid == orcid))).scalar_one_or_none()


def _hidden(body: str, field: str) -> str:
    """The value of the confirm form's hidden ``field``."""
    for tag in re.findall(r"<input\b[^>]*>", body):
        if re.search(rf"""\bname=["']{field}["']""", tag):
            match = re.search(r"""\bvalue=["']([^"']*)["']""", tag)
            if match:
                return html.unescape(match.group(1))
    raise AssertionError(f"no hidden {field!r} input in the page")


async def _preview(client, actor_id, orcid, role, name=""):
    return await client.post(
        _STAFF_PAGE, data={"orcid": orcid, "user_role": role, "name": name},
        headers=auth_headers(actor_id), follow_redirects=False,
    )


async def _confirm(client, actor_id, orcid, role, expectation, name=""):
    return await client.post(
        _STAFF_PAGE,
        data={"orcid": orcid, "user_role": role, "name": name, "confirm": "1",
              "expectation": expectation},
        headers=auth_headers(actor_id), follow_redirects=False,
    )


# --- the two-step add ---------------------------------------------------------------


@pytest.mark.parametrize(
    "actor_role,role",
    [(USER_ROLE_ADMIN, USER_ROLE_MANAGER), (USER_ROLE_ADMIN, USER_ROLE_REVIEWER),
     (USER_ROLE_MANAGER, USER_ROLE_REVIEWER)],
)
async def test_preview_writes_nothing_and_confirm_creates_the_account(
    client, db_session, orcid_record, actor_role, role,
):
    actor_id = await _user(db_session, user_role=actor_role)
    await db_session.commit()
    before = await _users(db_session)

    r = await _preview(client, actor_id, _NEW, role)
    assert r.status_code == 200
    body = html.unescape(r.text)
    assert "Jane Doe" in body
    assert "Johns Hopkins University" in body
    assert "Kennedy Krieger Institute" in body
    assert await _users(db_session) == before
    assert await _id_for(db_session, _NEW) is None

    r = await _confirm(client, actor_id, _NEW, role, _hidden(r.text, "expectation"))
    assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
    new_id = await _id_for(db_session, _NEW)
    assert new_id is not None
    state = await _state(db_session, new_id)
    assert (state.user_role, state.access_status) == (role, "allowed")
    agents = (await db_session.execute(
        select(func.count()).select_from(AgentRegistry).where(AgentRegistry.user_id == new_id)
    )).scalar_one()
    jobs = (await db_session.execute(
        select(func.count()).select_from(Job).where(Job.user_id == new_id)
    )).scalar_one()
    assert (agents, jobs) == (0, 0)


@pytest.mark.parametrize(
    "actor_role,access_status,with_profile,state_wording",
    [(USER_ROLE_MANAGER, "pending", False, ("access: pending", "no profile")),
     (USER_ROLE_ADMIN, "allowed", True, ("access: approved", "has a profile"))],
)
async def test_convert_preview_shows_the_account_and_confirm_converts(
    client, db_session, orcid_record, actor_role, access_status, with_profile, state_wording,
):
    actor_id = await _user(db_session, user_role=actor_role)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, access_status=access_status, orcid=_HELD,
        name="Pat Held",
    )
    pi_id = pi.id
    if with_profile:
        await factories.make_profile(db_session, user=pi)
    await db_session.commit()

    r = await _preview(client, actor_id, _HELD, USER_ROLE_REVIEWER)
    assert r.status_code == 200
    body = html.unescape(r.text)
    assert "Pat Held" in body
    for wording in state_wording:
        assert wording in body
    assert (await _state(db_session, pi_id)).user_role == USER_ROLE_PI

    r = await _confirm(client, actor_id, _HELD, USER_ROLE_REVIEWER, _hidden(r.text, "expectation"))
    assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
    state = await _state(db_session, pi_id)
    assert (state.user_role, state.access_status) == (USER_ROLE_REVIEWER, "allowed")


async def test_a_tampered_expectation_rerenders_and_writes_nothing(
    client, db_session, orcid_record,
):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.commit()
    before = (await _users(db_session), await _audits(db_session))

    r = await _confirm(client, admin_id, _NEW, USER_ROLE_REVIEWER, "tampered")
    assert r.status_code == 200
    assert "Jane Doe" in html.unescape(r.text)
    assert (await _users(db_session), await _audits(db_session)) == before


async def test_a_stale_preview_rerenders_the_fresh_one_and_writes_nothing(
    client, db_session, orcid_record,
):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.commit()
    before = await _users(db_session)

    r = await _preview(client, admin_id, _NEW, USER_ROLE_REVIEWER)
    expectation = _hidden(r.text, "expectation")
    orcid_record["name"] = "Janet Roe"

    r = await _confirm(client, admin_id, _NEW, USER_ROLE_REVIEWER, expectation)
    assert r.status_code == 200
    assert "Janet Roe" in html.unescape(r.text)
    assert _hidden(r.text, "expectation") != expectation
    assert await _users(db_session) == before


# --- refusals ------------------------------------------------------------------------


async def test_a_manager_cannot_create_a_manager(client, db_session, orcid_record):
    mgr_id = await _user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.commit()
    before = await _users(db_session)

    for confirm in ("", "1"):
        r = await client.post(
            _STAFF_PAGE,
            data={"orcid": _NEW, "user_role": USER_ROLE_MANAGER, "confirm": confirm,
                  "expectation": "x"},
            headers=auth_headers(mgr_id), follow_redirects=False,
        )
        assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
        flashes = session_from_response(r)[FLASH_SESSION_KEY]
        assert flashes[-1] == {"text": REFUSAL_MESSAGES["invalid_role"], "kind": "error"}
    assert await _users(db_session) == before


async def test_a_refusal_redirects_to_the_bare_path_and_refills_from_the_session(
    client, db_session, orcid_record,
):
    orcid_record["name"] = None
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.commit()

    r = await _preview(client, admin_id, _NEW, USER_ROLE_REVIEWER)
    assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
    session = session_from_response(r)
    assert session[STAFF_REFILL_SESSION_KEY] == {
        "orcid": _NEW, "user_role": USER_ROLE_REVIEWER, "name": "",
    }

    page = await client.get(_STAFF_PAGE, headers=raw_session_headers(session))
    assert page.status_code == 200
    body = html.unescape(page.text)
    assert REFUSAL_MESSAGES["name_required"] in body
    assert _NEW in body
    assert STAFF_REFILL_SESSION_KEY not in (session_from_response(page) or {})


async def test_an_invalid_refill_is_not_kept(client, db_session, orcid_record):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.commit()

    r = await client.post(
        _STAFF_PAGE, data={"orcid": "<script>", "user_role": USER_ROLE_ADMIN, "name": "x" * 300},
        headers=auth_headers(admin_id), follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
    assert session_from_response(r)[STAFF_REFILL_SESSION_KEY] == {
        "orcid": "", "user_role": "", "name": "x" * 100,
    }


async def test_an_admin_is_pointed_at_the_account_page(client, db_session, orcid_record):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, orcid=_HELD)
    pi_id = pi.id
    await factories.make_agent(db_session, user=pi, status="pending")
    await db_session.commit()

    r = await _preview(client, admin_id, _HELD, USER_ROLE_REVIEWER)
    assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
    session = session_from_response(r)
    assert f"/admin/users/{pi_id}" in session[FLASH_SESSION_KEY][-1]["text"]
    assert session[STAFF_REFILL_SESSION_KEY]["account_user_id"] == str(pi_id)


# --- who may post --------------------------------------------------------------------


def _posts(target_id):
    confirm = {"orcid": _NEW, "user_role": USER_ROLE_REVIEWER, "confirm": "1", "expectation": "x"}
    for prefix in ("/workspace", "/manager"):
        yield f"{prefix}/staff", {"orcid": _NEW, "user_role": USER_ROLE_REVIEWER}
        yield f"{prefix}/staff", confirm
        for action in ("revoke", "restore", "delete"):
            yield f"{prefix}/staff/{target_id}/{action}", {}


@pytest.mark.parametrize("who", ["pi", "reviewer", "anonymous", "impersonating"])
async def test_every_post_refuses_untrusted_identities(client, db_session, orcid_record, who):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    mgr_id = await _user(db_session, user_role=USER_ROLE_MANAGER)
    target_id = await _user(db_session, user_role=USER_ROLE_REVIEWER)
    pi_id = await _user(db_session, user_role=USER_ROLE_PI)
    await db_session.commit()
    headers = {
        "pi": auth_headers(pi_id),
        "reviewer": auth_headers(target_id),
        "anonymous": {},
        "impersonating": auth_headers(admin_id, impersonate=mgr_id),
    }[who]
    before = (await _users(db_session), await _audits(db_session), await _state(db_session, target_id))

    for path, data in _posts(target_id):
        r = await client.post(path, data=data, headers=headers, follow_redirects=False)
        if who == "anonymous":
            assert r.status_code == 302 and r.headers["location"].startswith("/login"), path
        else:
            assert r.status_code == 403, path
    after = (await _users(db_session), await _audits(db_session), await _state(db_session, target_id))
    assert after == before


# --- reviewer actions ----------------------------------------------------------------


@pytest.mark.parametrize("actor_role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER])
async def test_revoke_restore_delete_a_reviewer(client, db_session, actor_role):
    actor_id = await _user(db_session, user_role=actor_role)
    # Signed in with access: a manager may revoke only such a reviewer (D7).
    target_id = await _user(
        db_session, user_role=USER_ROLE_REVIEWER, last_login_at=datetime.now(UTC)
    )
    await db_session.commit()
    epoch = (await _state(db_session, target_id)).session_epoch

    async def act(action):
        r = await client.post(
            f"/workspace/staff/{target_id}/{action}", headers=auth_headers(actor_id),
            follow_redirects=False,
        )
        assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE

    await act("revoke")
    state = await _state(db_session, target_id)
    assert state.access_status == "denied" and state.session_epoch != epoch
    await act("restore")
    assert (await _state(db_session, target_id)).access_status == "allowed"
    await act("delete")
    assert await _state(db_session, target_id) is None


@pytest.mark.parametrize("actor_role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER])
async def test_reviewer_actions_redirect_for_a_pi_target(client, db_session, actor_role):
    actor_id = await _user(db_session, user_role=actor_role)
    pi_id = await _user(db_session, user_role=USER_ROLE_PI)
    await db_session.commit()
    before = await _state(db_session, pi_id)

    for action in ("revoke", "restore", "delete"):
        r = await client.post(
            f"/workspace/staff/{pi_id}/{action}", headers=auth_headers(actor_id),
            follow_redirects=False,
        )
        assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
        assert session_from_response(r)[FLASH_SESSION_KEY][-1]["kind"] == "error"
    assert await _state(db_session, pi_id) == before


async def test_revoke_ends_the_reviewers_existing_session(client, db_session):
    """The old session stays over after a restore: only the epoch bump can refuse it then,
    because the access check passes again once the reviewer is allowed."""
    mgr_id = await _user(db_session, user_role=USER_ROLE_MANAGER)
    rev_id = await _user(
        db_session, user_role=USER_ROLE_REVIEWER, last_login_at=datetime.now(UTC)
    )
    await db_session.commit()
    old_epoch = (await _state(db_session, rev_id)).session_epoch or 0
    old_cookie = session_headers(rev_id, epoch=old_epoch)
    assert (await client.get("/workspace/assessments", headers=old_cookie,
                             follow_redirects=False)).status_code == 200

    r = await client.post(f"/workspace/staff/{rev_id}/revoke", headers=auth_headers(mgr_id),
                          follow_redirects=False)
    assert r.status_code == 302

    r = await client.get("/workspace/assessments", headers=old_cookie, follow_redirects=False)
    assert r.status_code == 302
    assert not r.headers["location"].startswith("/workspace")

    r = await client.post(f"/workspace/staff/{rev_id}/restore", headers=auth_headers(mgr_id),
                          follow_redirects=False)
    assert r.status_code == 302
    state = await _state(db_session, rev_id)
    assert state.access_status == "allowed" and (state.session_epoch or 0) != old_epoch

    r = await client.get("/workspace/assessments", headers=old_cookie, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    fresh = session_headers(rev_id, epoch=state.session_epoch)
    r = await client.get("/workspace/assessments", headers=fresh, follow_redirects=False)
    assert r.status_code == 200


# --- first sign-in -------------------------------------------------------------------


def _fake_oauth(orcid: str):
    class _Client:
        async def fetch_token(self, url, code, grant_type):
            return {"orcid": orcid, "name": "Jane Doe"}

    return _Client()


@pytest.mark.parametrize(
    "role,landing",
    [(USER_ROLE_MANAGER, "/workspace/pis"), (USER_ROLE_REVIEWER, "/workspace/assessments")],
)
async def test_a_created_account_signs_in_to_its_role_page_and_queues_nothing(
    client, db_session, orcid_record, monkeypatch, role, landing,
):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.commit()
    r = await _preview(client, admin_id, _NEW, role)
    r = await _confirm(client, admin_id, _NEW, role, _hidden(r.text, "expectation"))
    assert r.status_code == 302
    new_id = await _id_for(db_session, _NEW)

    async def fake_profile(orcid_id):
        return {"orcid": orcid_id, "name": "Jane Doe"}

    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _fake_oauth(_NEW))
    monkeypatch.setattr(auth_module, "fetch_orcid_profile", fake_profile)
    r = await client.get(
        "/auth/callback?code=c&state=s",
        headers=raw_session_headers({"oauth_state": "s"}),
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == landing
    jobs = (await db_session.execute(
        select(func.count()).select_from(Job).where(Job.user_id == new_id)
    )).scalar_one()
    assert jobs == 0


# --- pages ---------------------------------------------------------------------------


async def test_legacy_manager_staff_redirects_staff_and_refuses_a_reviewer(client, db_session):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    mgr_id = await _user(db_session, user_role=USER_ROLE_MANAGER)
    rev_id = await _user(db_session, user_role=USER_ROLE_REVIEWER)
    await db_session.commit()

    for staff_id in (admin_id, mgr_id):
        r = await client.get("/manager/staff", headers=auth_headers(staff_id),
                             follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == _STAFF_PAGE
    r = await client.get("/manager/staff", headers=auth_headers(rev_id), follow_redirects=False)
    assert r.status_code == 403


async def test_the_staff_page_refuses_a_reviewer_and_a_pi(client, db_session):
    rev_id = await _user(db_session, user_role=USER_ROLE_REVIEWER)
    pi_id = await _user(db_session, user_role=USER_ROLE_PI)
    await db_session.commit()

    for user_id in (rev_id, pi_id):
        r = await client.get(_STAFF_PAGE, headers=auth_headers(user_id), follow_redirects=False)
        assert r.status_code == 403


async def test_the_pi_directory_points_staff_at_the_staff_page(client, db_session):
    admin_id = await _user(db_session, user_role=USER_ROLE_ADMIN)
    mgr_id = await _user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.commit()

    for user_id in (admin_id, mgr_id):
        body = (await client.get("/workspace/pis", headers=auth_headers(user_id))).text
        assert _STAFF_PAGE in body
        if user_id == mgr_id:
            assert "/admin/" not in body
