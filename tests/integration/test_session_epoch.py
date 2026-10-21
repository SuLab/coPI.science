"""Sessions are revocable server-side: a stale epoch is refused and cleared."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import AgentDelegate, DelegateInvitation
from src.services.session_epoch import bump_session_epoch
from tests import factories
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


async def _user(db_session, **overrides):
    user = await factories.make_user(db_session, **overrides)
    await factories.make_profile(db_session, user=user)
    await db_session.flush()
    return user


async def test_a_session_from_an_older_epoch_is_refused_and_cleared(client, db_session):
    user = await _user(db_session, session_epoch=1)
    stale = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert stale.status_code == 302 and stale.headers["location"] == "/login?next=%2Fprofile"
    assert session_from_response(stale) == {}
    current = await client.get("/profile", headers=session_headers(user.id, epoch=1))
    assert current.status_code == 200


async def test_a_session_with_no_epoch_key_counts_as_epoch_zero(client, db_session):
    user = await _user(db_session)
    assert (await client.get("/profile", headers=session_headers(user.id))).status_code == 200
    user.session_epoch = 1
    await db_session.flush()
    refused = await client.get("/profile", headers=session_headers(user.id))
    assert refused.headers["location"] == "/login?next=%2Fprofile"


@pytest.mark.parametrize("epoch", ["0", True, None, 0.0])
async def test_a_malformed_epoch_is_refused(client, db_session, epoch):
    user = await _user(db_session)
    r = await client.get("/profile", headers=session_headers(user.id, epoch=epoch))
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fprofile"


async def test_the_epoch_is_checked_on_the_session_account_not_the_impersonated_one(
    client, db_session
):
    admin = await _user(db_session, is_admin=True, session_epoch=1)
    target = await _user(db_session)
    r = await client.get(
        "/profile", headers=session_headers(admin.id, impersonate=target.id, epoch=0)
    )
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fprofile"
    assert session_from_response(r) == {}


async def test_a_denied_account_is_still_bounced_before_the_epoch_check(client, db_session):
    user = await _user(db_session, access_status="denied", session_epoch=1)
    r = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert session_from_response(r) == {}


async def test_bump_with_expected_epoch_only_moves_a_matching_epoch(db_session):
    user = await _user(db_session, session_epoch=2)
    await bump_session_epoch(db_session, user.id, expected_epoch=0)
    await db_session.refresh(user)
    assert user.session_epoch == 2
    await bump_session_epoch(db_session, user.id, expected_epoch=2)
    await db_session.refresh(user)
    assert user.session_epoch == 3


async def test_bump_without_expected_epoch_always_moves(db_session):
    user = await _user(db_session)
    assert user.session_epoch is None
    await bump_session_epoch(db_session, user.id)
    await db_session.refresh(user)
    assert user.session_epoch == 1
    await bump_session_epoch(db_session, user.id)
    await db_session.refresh(user)
    assert user.session_epoch == 2


async def _invite_for(db_session, delegate):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    invite = DelegateInvitation(
        agent_registry_id=agent.id,
        invited_by_user_id=pi.id,
        email=delegate.email,
        token=uuid.uuid4().hex,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db_session.add(invite)
    await db_session.flush()
    return invite


async def _delegate_rows(db_session, invite):
    result = await db_session.execute(
        select(AgentDelegate).where(AgentDelegate.invitation_id == invite.id)
    )
    return result.scalars().all()


async def test_a_revoked_session_cannot_accept_an_invite(client, db_session):
    user = await _user(db_session, session_epoch=1)
    invite = await _invite_for(db_session, user)

    stale = await client.post(
        f"/invite/{invite.token}/accept", headers=session_headers(user.id, epoch=0)
    )
    assert stale.status_code == 302 and stale.headers["location"] == f"/invite/{invite.token}"
    assert await _delegate_rows(db_session, invite) == []

    current = await client.post(
        f"/invite/{invite.token}/accept", headers=session_headers(user.id, epoch=1)
    )
    assert current.status_code == 302
    assert len(await _delegate_rows(db_session, invite)) == 1


async def test_a_revoked_session_viewing_an_invite_is_sent_to_login_with_the_token_kept(
    client, db_session
):
    user = await _user(db_session, session_epoch=1)
    invite = await _invite_for(db_session, user)

    r = await client.get(f"/invite/{invite.token}", headers=session_headers(user.id, epoch=0))

    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert session_from_response(r) == {"pending_invite_token": invite.token}
