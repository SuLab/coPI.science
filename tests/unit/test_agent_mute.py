"""Mute/unmute: the muted_at/muted_by columns, and set_agent_mute_state."""
from datetime import UTC, datetime

import pytest

from src.services.agent_mute import set_agent_mute_state
from tests import factories

pytestmark = pytest.mark.integration


async def test_agent_registry_has_mute_tracking_columns(db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="active")
    assert agent.muted_at is None
    assert agent.muted_by is None


async def test_muting_an_active_agent_sets_inactive_and_attribution(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(db_session, user=pi, status="active")

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=True, actor_user_id=manager.id,
    )

    assert ok is True
    await db_session.refresh(agent)
    assert agent.status == "inactive"
    assert agent.muted_by == manager.id
    assert agent.muted_at is not None


async def test_unmuting_clears_attribution_and_reactivates(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(
        db_session, user=pi, status="inactive",
        muted_at=datetime.now(UTC), muted_by=manager.id,
    )

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=False, actor_user_id=manager.id,
    )

    assert ok is True
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.muted_by is None
    assert agent.muted_at is None


async def test_muting_a_pending_agent_is_a_no_op(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(db_session, user=pi, status="pending")

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=True, actor_user_id=manager.id,
    )

    assert ok is False
    await db_session.refresh(agent)
    assert agent.status == "pending"
    assert agent.muted_at is None


async def test_muting_a_suspended_agent_is_a_no_op(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(db_session, user=pi, status="suspended")

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=True, actor_user_id=manager.id,
    )

    assert ok is False
    await db_session.refresh(agent)
    assert agent.status == "suspended"


async def test_unmuting_a_pending_agent_cannot_activate_it(db_session):
    """Pending rows are common now (the manager Add-PI flow auto-creates
    them), and unmute writes status='active' — this pin is what keeps the
    manager's unmute button from becoming a side door around the admin
    activation gate."""
    pi = await factories.make_user(db_session, orcid="0000-0021-0000-0001")
    agent = await factories.make_agent(db_session, user=pi, status="pending")
    changed = await set_agent_mute_state(
        db_session, agent=agent, muted=False, actor_user_id=pi.id,
    )
    assert changed is False
    assert agent.status == "pending"
