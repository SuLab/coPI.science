"""Mute/unmute: the muted_at/muted_by columns, and set_agent_mute_state."""
import pytest

from src.models import AgentRegistry
from tests import factories

pytestmark = pytest.mark.integration


async def test_agent_registry_has_mute_tracking_columns(db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="active")
    assert agent.muted_at is None
    assert agent.muted_by is None
