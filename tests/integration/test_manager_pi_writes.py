"""POST routes on /manager: create, edit, mute, unmute a PI (design D1)."""
from unittest.mock import AsyncMock, patch

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


async def test_pi_is_denied_all_four_write_routes(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    target = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    headers = auth_headers(pi.id)

    r = await client.post("/manager/pis", data={"orcid": "0000-0003-0000-0000"}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/profile", data={}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/mute", headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/unmute", headers=headers)
    assert r.status_code == 403


async def test_manager_creates_a_pi_via_orcid(client, db_session):
    manager = await _manager(db_session)
    with patch(
        "src.routers.manager.find_or_create_pi_by_orcid",
        new=AsyncMock(),
    ) as mock_create:
        mock_create.return_value = User(
            id="00000000-0000-0000-0000-000000000001", orcid="0000-0004-0000-0000",
        )
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0004-0000-0000"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302
    assert "/manager/pis/" in r.headers["location"]


async def test_manager_create_pi_rejects_a_duplicate_orcid(client, db_session):
    manager = await _manager(db_session)
    existing = await factories.make_user(db_session, orcid="0000-0005-0000-0000")

    r = await client.post(
        "/manager/pis", data={"orcid": existing.orcid},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_manager_edits_a_pi_profile(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, name="Old Name")

    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={
            "name": "New Name", "email": pi.email or "", "institution": "",
            "department": "", "research_summary": "Edited.", "techniques": "",
            "experimental_models": "", "disease_areas": "", "key_targets": "",
            "keywords": "",
        },
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(pi)
    assert pi.name == "New Name"


async def test_manager_edit_404s_on_a_non_pi_target(client, db_session):
    manager = await _manager(db_session)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    r = await client.post(
        f"/manager/pis/{other_admin.id}/profile", data={}, headers=auth_headers(manager.id),
    )
    assert r.status_code == 404


async def test_manager_mutes_and_unmutes_a_pi(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="active")

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.status == "inactive"
    assert agent.muted_by == manager.id

    r = await client.post(
        f"/manager/pis/{pi.id}/unmute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.muted_by is None


async def test_muting_a_pi_with_no_agent_redirects_with_an_error(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_muting_a_pending_agent_redirects_with_an_error(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="pending")

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]
