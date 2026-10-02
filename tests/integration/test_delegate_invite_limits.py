"""SN-02: at most 10 addresses per submission and 25 invitations per agent per
rolling 24 h, counted from delegate_invitations rows of any status (so revoke and
resend cannot get around it). D-21: an invitation whose email was not sent says so."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.models import DelegateInvitation
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _owner(db):
    pi = await factories.make_user(db, name="Cap Pi")
    agent = await factories.make_agent(db, user=pi, status="active")
    return pi, agent


async def _count(db, agent):
    return (await db.execute(
        select(func.count(DelegateInvitation.id))
        .where(DelegateInvitation.agent_registry_id == agent.id)
    )).scalar_one()


async def _prior(db, pi, agent, n, *, status="pending", created_at=None):
    for _ in range(n):
        row = DelegateInvitation(
            agent_registry_id=agent.id, invited_by_user_id=pi.id,
            email=f"prior-{uuid.uuid4().hex[:8]}@example.org", token=uuid.uuid4().hex,
            status=status, expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
    await db.flush()


async def _invite(client, pi, agent, emails):
    return await client.post(
        f"/agent/{agent.agent_id}/delegates/invite", data={"emails": emails},
        headers=auth_headers(pi.id), follow_redirects=False,
    )


async def test_more_than_ten_addresses_are_refused_whole(client, db_session):
    pi, agent = await _owner(db_session)
    r = await _invite(client, pi, agent, ",".join(f"cap{i}@example.org" for i in range(11)))
    assert r.status_code == 302
    assert await _count(db_session, agent) == 0
    page = await follow(client, r)
    assert "At most 10 addresses per invitation" in page.text


async def test_ten_addresses_are_accepted(client, db_session):
    pi, agent = await _owner(db_session)
    await _invite(client, pi, agent, "\n".join(f"ten{i}@example.org" for i in range(10)))
    assert await _count(db_session, agent) == 10


async def test_the_daily_cap_stops_at_25_per_agent(client, db_session):
    pi, agent = await _owner(db_session)
    await _prior(db_session, pi, agent, 24)
    r = await _invite(client, pi, agent, "a1@example.org,a2@example.org,a3@example.org")
    assert await _count(db_session, agent) == 25
    page = await follow(client, r)
    assert "Daily limit of 25 invitations per agent reached" in page.text
    assert "a2@example.org was not invited" in page.text


async def test_revoked_invitations_still_count(client, db_session):
    pi, agent = await _owner(db_session)
    await _prior(db_session, pi, agent, 25, status="revoked")
    await _invite(client, pi, agent, "late@example.org")
    assert await _count(db_session, agent) == 25


async def test_invitations_older_than_a_day_do_not_count(client, db_session):
    pi, agent = await _owner(db_session)
    await _prior(
        db_session, pi, agent, 25, status="expired",
        created_at=datetime.now(UTC) - timedelta(hours=25),
    )
    await _invite(client, pi, agent, "fresh@example.org")
    assert await _count(db_session, agent) == 26


async def test_an_unsent_invitation_email_is_reported_and_escaped(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.routers.agent_page.send_transactional_email", AsyncMock(return_value=False)
    )
    pi, agent = await _owner(db_session)
    r = await _invite(client, pi, agent, "<b>tag</b>@example.org")
    assert await _count(db_session, agent) == 1
    page = await follow(client, r)
    assert "no email was sent to" in page.text
    assert "&lt;b&gt;tag&lt;/b&gt;@example.org" in page.text
    assert "<b>tag</b>@example.org" not in page.text
