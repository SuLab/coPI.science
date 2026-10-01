"""RB-10 / RB-11: double submits are tolerated. Each test fires two concurrent calls
through separate committed sessions, and deletes the rows it created."""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from src.models import AgentDelegate, AgentRegistry, DelegateInvitation, User
from tests import factories

pytestmark = pytest.mark.integration


async def _committed(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        pi = await factories.make_user(s, name="Inv Pi", email=f"pi{uuid.uuid4().hex[:6]}@x.edu")
        d = await factories.make_user(s, name="Del", email=f"d{uuid.uuid4().hex[:6]}@x.edu")
        agent = await factories.make_agent(s, user=pi, agent_id=f"inv{uuid.uuid4().hex[:6]}", status="active")
        inv = DelegateInvitation(agent_registry_id=agent.id, invited_by_user_id=pi.id, email=d.email,
                                 token=uuid.uuid4().hex, status="pending",
                                 expires_at=datetime.now(UTC) + timedelta(days=1))
        s.add(inv)
        await s.commit()
    return f, pi, d, agent, inv


async def _cleanup(f, users, agent_ids):
    async with f() as s:
        for aid in agent_ids:
            await s.execute(text("DELETE FROM agents WHERE id = :a"), {"a": aid})
        for uid in users:
            await s.execute(text("DELETE FROM agents WHERE user_id = :u"), {"u": uid})
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await s.commit()


async def test_double_accept_creates_one_delegate(engine, monkeypatch):
    """Review Focus 3."""
    from src.routers import invite as invite_routes

    async def lookup(token, email):
        return None

    monkeypatch.setattr("src.services.slack_web.lookup_user_by_email_async", lookup)
    f, pi, d, agent, inv = await _committed(engine)
    try:
        async def accept():
            async with f() as s:
                row = await s.get(DelegateInvitation, inv.id)
                user = await s.get(User, d.id)
                return await invite_routes._accept_invitation(row, user, s, request=None)

        results = await asyncio.gather(accept(), accept(), return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)]
        async with f() as s:
            n = (await s.execute(select(func.count()).select_from(AgentDelegate)
                                 .where(AgentDelegate.agent_registry_id == agent.id))).scalar_one()
        assert n == 1
    finally:
        await _cleanup(f, [pi.id, d.id], [agent.id])


async def test_double_request_agent_creates_one_agent(engine):
    from src.routers import agent_page as routes

    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        pi = await factories.make_user(s, name="Dup Request")
        await factories.make_profile(s, user=pi)
        await s.commit()
    try:
        async def request():
            async with f() as s:
                user = (await s.execute(select(User).options(selectinload(User.profile))
                                        .where(User.id == pi.id))).scalar_one()
                return await routes.request_agent(request=None, db=s, current_user=user)

        results = await asyncio.gather(request(), request(), return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert all(r.headers["location"] == "/agent" for r in results)
        async with f() as s:
            n = (await s.execute(select(func.count()).select_from(AgentRegistry)
                                 .where(AgentRegistry.user_id == pi.id))).scalar_one()
        assert n == 1
    finally:
        await _cleanup(f, [pi.id], [])


async def test_double_invite_creates_one_invitation_and_one_email(engine, monkeypatch):
    from src.routers import agent_page as routes

    sent = []

    async def fake_send(message, *, force=False):
        sent.append(message.to)
        return True

    monkeypatch.setattr("src.routers.agent_page.send_transactional_email", fake_send)
    f, pi, d, agent, _inv = await _committed(engine)
    try:
        async def invite():
            async with f() as s:
                user = await s.get(User, pi.id)
                return await routes.invite_delegate(
                    agent.agent_id, request=None, emails="fresh@x.edu", db=s, current_user=user)

        results = await asyncio.gather(invite(), invite(), return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)], results
        async with f() as s:
            n = (await s.execute(select(func.count()).select_from(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == agent.id,
                DelegateInvitation.email == "fresh@x.edu",
                DelegateInvitation.status == "pending"))).scalar_one()
        assert n == 1
        assert sent == ["fresh@x.edu"]
    finally:
        await _cleanup(f, [pi.id, d.id], [agent.id])
