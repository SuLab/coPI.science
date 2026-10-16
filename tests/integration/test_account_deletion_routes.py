"""Route-level deletion guards (audit F7/F8, decision D6).

The test DB is savepoint-isolated per test (tests/conftest.py::db_session), so
nothing here leaks across tests.
"""
import base64
import json
from types import SimpleNamespace

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import func, select, update

from src.config import get_settings
from src.models import Publication, User
from src.services import user_deletion
from src.services.user_deletion import research_data_for
from tests import factories
from tests.session_support import session_cookie_name, session_headers

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def teardown_dirs(tmp_path, monkeypatch):
    """Redirect the teardown's file cleanup away from the repo's profiles/."""
    pub = tmp_path / "public"
    mem = tmp_path / "memory"
    monkeypatch.setattr(user_deletion, "_PUBLIC_DIR", pub)
    monkeypatch.setattr(user_deletion, "_MEMORY_DIR", mem)
    return SimpleNamespace(public=pub, memory=mem)


def _auth(user_id) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"{session_cookie_name()}={signer.sign(data).decode()}"}


def _auth_as(admin_id, impersonate_id) -> dict:
    """Admin session with an impersonation of ``impersonate_id`` signed into it."""
    return session_headers(admin_id, impersonate=impersonate_id)


async def test_impersonated_delete_is_refused(client, db_session):
    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    r = await client.post(
        "/profile/delete-account",
        headers=_auth_as(admin.id, pi.id),
        data={"confirm": "delete"},
    )
    assert r.status_code == 403
    assert (await db_session.get(User, pi.id)) is not None


async def test_last_admin_cannot_self_delete(client, db_session):
    # db_session is savepoint-isolated, but SOME suites commit real rows
    # through their own sessionmaker (test_worker.py, test_role_live_flip.py),
    # and a reused TEST_DATABASE_URL scratch DB can hold debris from a crashed
    # run. Demote any pre-existing loginable admins HERE (rolls back with the
    # savepoint) so the guard under test is what decides the outcome.
    debris = (
        await db_session.execute(
            select(User).where(
                User.user_role == "admin", User.access_status == "allowed"
            )
        )
    ).scalars().all()
    for u in debris:
        u.access_status = "denied"
    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    await db_session.flush()

    r = await client.post(
        "/profile/delete-account",
        headers=_auth(admin.id),
        data={"confirm": "delete"},
    )
    assert r.status_code == 302
    assert "error=last_admin" in r.headers["location"]
    assert (await db_session.get(User, admin.id)) is not None


async def test_admin_with_peers_can_self_delete(client, db_session):
    a1 = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    a2 = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    r = await client.post(
        "/profile/delete-account", headers=_auth(a1.id), data={"confirm": "delete"}
    )
    assert r.status_code == 302
    assert "deleted=1" in r.headers["location"]
    assert (await db_session.get(User, a1.id)) is None
    assert (await db_session.get(User, a2.id)) is not None


async def test_pi_delete_routes_through_teardown(client, db_session):
    """The route calls the service: a linked agent ends the request suspended."""
    from src.models import AgentRegistry

    pi = await factories.make_user(db_session, access_status="allowed")
    agent = await factories.make_agent(db_session, user=pi, status="active")
    agent_pk = agent.id
    r = await client.post(
        "/profile/delete-account", headers=_auth(pi.id), data={"confirm": "delete"}
    )
    assert r.status_code == 302
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.status == "suspended"


async def test_admin_delete_removes_allowlist_when_asked(client, db_session):
    from src.models import AccessAllowlist

    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    db_session.add(AccessAllowlist(orcid=pi.orcid))
    await db_session.flush()
    orcid = pi.orcid

    r = await client.post(
        f"/admin/users/{pi.id}/delete",
        headers=_auth(admin.id),
        data={"remove_from_allowlist": "1"},
    )
    assert r.status_code == 302
    assert (
        await db_session.scalar(
            select(AccessAllowlist).where(AccessAllowlist.orcid == orcid)
        )
    ) is None


async def test_admin_delete_keeps_allowlist_by_default(client, db_session):
    from src.models import AccessAllowlist

    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    db_session.add(AccessAllowlist(orcid=pi.orcid))
    await db_session.flush()
    orcid = pi.orcid

    r = await client.post(
        f"/admin/users/{pi.id}/delete", headers=_auth(admin.id), data={}
    )
    assert r.status_code == 302
    assert (
        await db_session.scalar(
            select(AccessAllowlist).where(AccessAllowlist.orcid == orcid)
        )
    ) is not None


async def test_admin_delete_suspends_linked_agent(client, db_session):
    from src.models import AgentRegistry

    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    agent = await factories.make_agent(db_session, user=pi, status="active")
    agent_pk = agent.id

    r = await client.post(
        f"/admin/users/{pi.id}/delete", headers=_auth(admin.id), data={}
    )
    assert r.status_code == 302
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.status == "suspended"


async def test_admin_delete_is_refused_while_impersonating(client, db_session):
    """Admin-on-admin impersonation must not reach the admin delete route:
    the self-delete guard and the actor log line both key on current_user,
    which impersonation substitutes (final-review Important #2)."""
    real_admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    other_admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")

    r = await client.post(
        f"/admin/users/{pi.id}/delete",
        headers=_auth_as(real_admin.id, other_admin.id),
        data={},
    )
    assert r.status_code == 403
    assert (await db_session.get(User, pi.id)) is not None


async def _give(db, user_id, kind: str) -> None:
    if kind == "profile":
        await factories.make_profile(db, user_id=user_id)
    elif kind == "publication":
        db.add(Publication(user_id=user_id, pmid="31000001", title="A stored paper"))
        await db.flush()
    else:
        await factories.make_agent(db, user_id=user_id, status="suspended")


@pytest.mark.parametrize("role", ["reviewer", "manager"])
@pytest.mark.parametrize("kind", ["profile", "publication", "agent"])
async def test_a_non_pi_with_research_data_cannot_delete_their_own_account(
    client, db_session, role, kind,
):
    user = await factories.make_user(db_session, user_role=role, access_status="allowed")
    uid = user.id
    await _give(db_session, uid, kind)
    await db_session.commit()
    r = await client.post("/profile/delete-account", headers=_auth(uid), data={"confirm": "delete"})
    assert r.status_code == 302
    assert r.headers["location"] == "/profile/delete-account?error=research_data"
    # Review Focus 5: the refusal writes nothing, the session included (a cleared Starlette
    # session is sent back as an expired cookie).
    set_cookie = r.headers.get("set-cookie", "")
    assert "1970" not in set_cookie and "=null" not in set_cookie
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == uid)) == 1
    assert (await research_data_for(db_session, [uid]))[uid].has_any


async def _stale(asgi_app, db_session, uid, role):
    """Serve the request a user whose in-memory role is ``role`` while the row says
    otherwise: a role change committed after the request loaded the user."""
    from sqlalchemy.orm.attributes import set_committed_value

    from src.dependencies import get_current_user

    loaded = await db_session.get(User, uid)
    set_committed_value(loaded, "user_role", role)
    asgi_app.dependency_overrides[get_current_user] = lambda: loaded


async def test_a_conversion_after_the_request_loaded_the_user_is_still_refused(
    client, asgi_app, db_session,
):
    """The request loaded a PI; a staff conversion committed before the guard ran. The role
    is re-read under the row lock, so the now-manager account is still refused (F18)."""
    user = await factories.make_user(db_session, user_role="manager", access_status="allowed")
    uid = user.id
    await _give(db_session, uid, "publication")
    await db_session.commit()
    await _stale(asgi_app, db_session, uid, "pi")
    r = await client.post("/profile/delete-account", headers=_auth(uid), data={"confirm": "delete"})
    assert r.headers["location"] == "/profile/delete-account?error=research_data"
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == uid)) == 1


async def test_a_promotion_after_the_request_loaded_the_user_keeps_the_last_admin(
    client, asgi_app, db_session,
):
    """The last-admin check decides on the re-read role too: an account promoted to the only
    admin after the request loaded it as a PI is not deleted."""
    await db_session.execute(
        update(User).where(User.user_role == "admin").values(access_status="denied"))
    user = await factories.make_user(db_session, user_role="admin", access_status="allowed")
    uid = user.id
    await db_session.commit()
    await _stale(asgi_app, db_session, uid, "pi")
    r = await client.post("/profile/delete-account", headers=_auth(uid), data={"confirm": "delete"})
    assert r.headers["location"] == "/profile/delete-account?error=last_admin"
    assert await db_session.get(User, uid) is not None


async def test_a_non_pi_without_research_data_still_deletes_their_own_account(client, db_session):
    rev = await factories.make_user(db_session, user_role="reviewer", access_status="allowed")
    uid = rev.id
    await db_session.commit()
    r = await client.post("/profile/delete-account", headers=_auth(uid), data={"confirm": "delete"})
    assert r.status_code == 302 and r.headers["location"] == "/login?deleted=1"
    assert (await db_session.get(User, uid)) is None


async def test_a_pi_with_research_data_still_deletes_their_own_account(client, db_session):
    pi = await factories.make_user(db_session, access_status="allowed")
    uid = pi.id
    await _give(db_session, uid, "profile")
    await _give(db_session, uid, "publication")
    await db_session.commit()
    r = await client.post("/profile/delete-account", headers=_auth(uid), data={"confirm": "delete"})
    assert r.status_code == 302 and r.headers["location"] == "/login?deleted=1"
