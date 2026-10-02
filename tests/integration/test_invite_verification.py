"""Invite acceptance (spec 2026-10-01 §6.6; A-04, A-09, D-06): the user comes from
get_current_user, must be a PI-surface account, and must hold the invited address,
verified."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AgentDelegate,
    DelegateInvitation,
)
from src.routers.invite import _INVITE_NOT_PI_MSG, _INVITE_UNVERIFIED_MSG
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


async def _invitation(db_session, email: str):
    inviter = await factories.make_user(db_session, name="Inviting PI")
    agent = await factories.make_agent(db_session, user=inviter)
    token = uuid.uuid4().hex
    db_session.add(DelegateInvitation(
        agent_registry_id=agent.id, invited_by_user_id=inviter.id, email=email, token=token,
        status="pending", expires_at=datetime.now(UTC) + timedelta(days=1),
    ))
    await db_session.flush()
    return agent, token


async def _delegates(db_session, agent):
    return (await db_session.execute(
        select(AgentDelegate.user_id).where(AgentDelegate.agent_registry_id == agent.id)
    )).scalars().all()


async def test_a_verified_pi_holding_the_invited_address_accepts(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org",
                                    email_verified_at=datetime.now(UTC))
    page = await client.get(f"/invite/{token}", headers=auth_headers(dee.id))
    assert page.status_code == 200 and f'action="/invite/{token}/accept"' in page.text
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(dee.id))
    assert r.status_code == 302 and r.headers["location"] == f"/agent/{agent.agent_id}/dashboard"
    assert await _delegates(db_session, agent) == [dee.id]


async def test_the_address_match_is_case_insensitive(client, db_session):
    agent, token = await _invitation(db_session, "Dee@Example.org")
    dee = await factories.make_user(db_session, email="dee@example.org",
                                    email_verified_at=datetime.now(UTC))
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(dee.id))
    assert r.status_code == 302
    assert await _delegates(db_session, agent) == [dee.id]


async def test_an_unverified_address_is_refused(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org")
    page = await client.get(f"/invite/{token}", headers=auth_headers(dee.id))
    assert page.status_code == 200 and _INVITE_UNVERIFIED_MSG in page.text
    assert f'action="/invite/{token}/accept"' not in page.text
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(dee.id))
    assert r.status_code == 200 and _INVITE_UNVERIFIED_MSG in r.text
    assert await _delegates(db_session, agent) == []


async def test_a_different_address_is_still_refused(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    other = await factories.make_user(db_session, email="other@example.org",
                                      email_verified_at=datetime.now(UTC))
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(other.id))
    assert r.status_code == 200 and "different email address" in r.text
    assert await _delegates(db_session, agent) == []


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_a_staff_or_reviewer_account_cannot_accept(client, db_session, role):
    agent, token = await _invitation(db_session, "staff@example.org")
    user = await factories.make_user(db_session, user_role=role, email="staff@example.org",
                                     email_verified_at=datetime.now(UTC))
    page = await client.get(f"/invite/{token}", headers=auth_headers(user.id))
    assert _INVITE_NOT_PI_MSG in page.text
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(user.id))
    assert _INVITE_NOT_PI_MSG in r.text
    assert await _delegates(db_session, agent) == []


async def test_a_denied_account_is_bounced_like_any_page(client, db_session):
    _agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org", access_status="denied",
                                    email_verified_at=datetime.now(UTC))
    r = await client.get(f"/invite/{token}", headers=auth_headers(dee.id))
    assert r.status_code == 302 and r.headers["location"] == "/access-pending"


async def test_a_signed_out_session_goes_to_login_and_comes_back(client, db_session):
    _agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org", session_epoch=1,
                                    email_verified_at=datetime.now(UTC))
    r = await client.get(f"/invite/{token}", headers=session_headers(dee.id, epoch=0))
    assert r.status_code == 302
    assert r.headers["location"] == f"/login?next=%2Finvite%2F{token}"


async def test_an_impersonating_admin_cannot_accept_for_the_pi(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, user_role=USER_ROLE_PI, email="dee@example.org",
                                    email_verified_at=datetime.now(UTC))
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    headers = auth_headers(admin.id, impersonate=dee.id)
    assert (await client.get(f"/invite/{token}", headers=headers)).status_code == 403
    assert (await client.post(f"/invite/{token}/accept", headers=headers)).status_code == 403
    assert await _delegates(db_session, agent) == []


async def test_an_anonymous_visitor_is_sent_to_sign_in_with_the_token_kept(client, db_session):
    _agent, token = await _invitation(db_session, "dee@example.org")
    r = await client.get(f"/invite/{token}")
    assert r.status_code == 302 and r.headers["location"] == "/login/start"
    assert session_from_response(r) == {"pending_invite_token": token}
