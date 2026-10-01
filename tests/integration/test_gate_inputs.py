from pathlib import Path

import pytest

from src.services.cohort_gate_inputs import load_gate_inputs
from tests import factories

pytestmark = pytest.mark.integration


async def test_roster_is_the_engine_roster_query(db_session):
    linked = await factories.make_agent(db_session, user=await factories.make_user(db_session), status="active")
    orphan = await factories.make_agent(db_session, status="active", role="pi_lab")  # no user: excluded
    hub = await factories.make_agent(db_session, status="active", role="scout_hub")
    inactive = await factories.make_agent(db_session, user=await factories.make_user(db_session), status="inactive")
    got = await load_gate_inputs(db_session)
    assert linked.agent_id in got.agent_ids and hub.agent_id in got.agent_ids
    assert orphan.agent_id not in got.agent_ids and inactive.agent_id not in got.agent_ids
    assert got.agent_ids == sorted(got.agent_ids)


async def test_viewer_widening(db_session):
    inactive = await factories.make_agent(db_session, user=await factories.make_user(db_session), status="inactive")
    got = await load_gate_inputs(db_session, extra_agent_ids=[inactive.agent_id])
    assert inactive.agent_id in got.agent_ids


def test_both_web_callers_use_it():
    root = Path(__file__).resolve().parents[2]
    for rel in ("src/routers/admin/cohorts.py", "src/services/conversation_feed.py"):
        text = (root / rel).read_text()
        assert "load_gate_inputs(" in text
        assert 'AgentRegistry.status == "active"' not in text
