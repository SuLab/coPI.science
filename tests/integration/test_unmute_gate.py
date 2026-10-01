# tests/integration/test_unmute_gate.py
import pytest

from src.models import USER_ROLE_MANAGER
from src.services.agent_mute import set_agent_mute_state
from tests import factories

pytestmark = pytest.mark.integration


async def test_unmute_refused_without_a_grounded_profile(db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="inactive", slack_bot_token="xoxb-1")
    assert await set_agent_mute_state(db_session, agent=agent, muted=False, actor=manager) == "activation_blocked"
    await db_session.refresh(agent)
    assert agent.status == "inactive"


async def test_unmute_refused_without_a_token(db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, evidence_state="grounded")
    agent = await factories.make_agent(db_session, user=pi, status="inactive", slack_bot_token=None)
    assert await set_agent_mute_state(db_session, agent=agent, muted=False, actor=manager) == "no_token"


async def test_unmute_is_conditional_on_status(db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, evidence_state="grounded")
    agent = await factories.make_agent(db_session, user=pi, status="suspended", slack_bot_token="xoxb-1")
    assert await set_agent_mute_state(db_session, agent=agent, muted=False, actor=manager) == "agent_not_mutable"


async def test_unmute_succeeds_through_the_gate(db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, evidence_state="grounded")
    agent = await factories.make_agent(db_session, user=pi, status="inactive", slack_bot_token="xoxb-1")
    assert await set_agent_mute_state(db_session, agent=agent, muted=False, actor=manager) is None
    await db_session.refresh(agent)
    assert (agent.status, agent.muted_at, agent.muted_by) == ("active", None, None)
