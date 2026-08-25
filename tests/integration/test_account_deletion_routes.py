"""Route-level deletion guards (audit F7/F8, decision D6).

The test DB is savepoint-isolated per test (tests/conftest.py::db_session), so
each test starts from an empty users table and nothing here leaks across tests.
"""
import base64
import json
from types import SimpleNamespace

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import User
from src.services import user_deletion
from tests import factories

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
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _auth_as(admin_id, impersonate_id) -> dict:
    """Admin session plus the copi-impersonate cookie.

    Byte-identical to tests/integration/test_onboarding_flow.py:73 — the
    impersonate cookie is a PLAIN unsigned UUID (src/dependencies.py reads it
    with uuid.UUID(cookie_value), no signer).
    """
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(admin_id)}).encode())
    return {
        "Cookie": (
            f"copi-session={signer.sign(data).decode()}; "
            f"copi-impersonate={impersonate_id}"
        )
    }


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
