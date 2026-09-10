"""The shared `activate_agent` service (task 8, F2).

`admin_approve_agent` and the manager surface both need "check the gate, then
flip to active" as one atomic unit rather than two call sites re-deriving the
same sequence — see `src/services/agent_activation.py`'s module docstring for
why the gate exists at all.
"""

from datetime import UTC, datetime

import pytest

from src.models import USER_ROLE_ADMIN
from src.services.agent_activation import activate_agent
from tests import factories

pytestmark = pytest.mark.integration


async def test_activate_agent_refuses_without_override_and_activates_with_it(
    db_session,
):
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=user, status="pending")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    blockers = await activate_agent(db_session, agent, actor=admin, override=False)
    assert blockers
    assert agent.status == "pending"
    assert agent.approved_by is None

    assert (
        await activate_agent(db_session, agent, actor=admin, override=True) == []
    )
    assert agent.status == "active"
    assert agent.approved_by == admin.id
    assert agent.approved_at is not None


async def test_activate_agent_activates_clean_agent_without_override(db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, evidence_pub_count=1)
    agent = await factories.make_agent(db_session, user=user, status="pending")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    blockers = await activate_agent(db_session, agent, actor=admin, override=False)

    assert blockers == []
    assert agent.status == "active"
    assert agent.approved_by == admin.id


async def test_reactivating_a_muted_agent_preserves_the_original_approval(
    db_session,
):
    """``approved_at``/``approved_by`` are the record of who first vouched for
    this agent, not of who last unmuted it. The pre-refactor admin handler
    stamped them only on the pending→active transition; re-activating from
    ``inactive`` (a manager unmute) or ``suspended`` must leave the original
    provenance standing.
    """
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, evidence_pub_count=1)
    original_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    original_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    agent = await factories.make_agent(db_session, user=user, status="inactive")
    agent.approved_by = original_admin.id
    agent.approved_at = original_at
    await db_session.flush()

    later_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    assert await activate_agent(
        db_session, agent, actor=later_admin, override=False
    ) == []

    assert agent.status == "active"
    assert agent.approved_by == original_admin.id
    assert agent.approved_at == original_at
