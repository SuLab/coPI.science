"""The single roster criterion (audit F1, decision D7): active, and for
pi_lab rows, linked to a user. Hub/specialist roles carry no user by design."""
import pytest

from src.agent.roster_query import active_roster_select
from tests import factories

pytestmark = pytest.mark.asyncio


async def test_orphaned_pi_lab_is_excluded(db_session):
    user = await factories.make_user(db_session)
    linked = await factories.make_agent(db_session, user=user, status="active")
    orphan = await factories.make_agent(db_session, status="active")  # user_id NULL
    hub = await factories.make_agent(db_session, status="active", role="scout_hub")
    suspended = await factories.make_agent(
        db_session, user=await factories.make_user(db_session), status="suspended"
    )

    rows = (await db_session.execute(active_roster_select())).all()
    ids = {r.agent_id for r in rows}

    assert linked.agent_id in ids
    assert hub.agent_id in ids  # NULL user is fine for non-pi_lab roles
    assert orphan.agent_id not in ids
    assert suspended.agent_id not in ids


async def test_select_carries_the_five_roster_columns(db_session):
    user = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=user, status="active")
    row = (await db_session.execute(active_roster_select())).first()
    for col in ("agent_id", "bot_name", "pi_name", "slack_bot_token", "role"):
        assert hasattr(row, col)
