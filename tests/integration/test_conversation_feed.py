"""The conversations feed's visibility gate, and its parity with the engine.

The page must show exactly what the viewing agent's bot is allowed to act on.
The engine decides that in memory (``_entry_allowed``); the page decides it in
SQL (``gate_clause``). Two implementations of one rule is a drift hazard, so the
parity test below drives BOTH from the engine's own ``DECISION_TABLE``.
"""

import pytest
from sqlalchemy import select

from src.agent.message_log import _entry_allowed
from src.models import AgentMessage, Cohort, CohortMembership
from src.services.conversation_feed import gate_clause, resolve_agent_gate
from tests import factories
from tests.unit.test_cohort_isolation import DECISION_TABLE, _post

pytestmark = pytest.mark.integration


async def _cohort(db, name, *agent_ids):
    c = Cohort(name=name)
    db.add(c)
    await db.flush()
    for aid in agent_ids:
        db.add(CohortMembership(cohort_id=c.id, agent_id=aid))
    await db.flush()
    return c


@pytest.mark.parametrize(
    "name,kwargs,gate,expected", DECISION_TABLE, ids=[r[0] for r in DECISION_TABLE]
)
async def test_gate_clause_matches_entry_allowed(
    db_session, name, kwargs, gate, expected
):
    """Every row of the engine's §5.1 table, decided by SQL instead of Python."""
    run = await factories.make_simulation_run(db_session)
    row_kwargs = dict(agent_id="x", is_bot=True, visibility="public")
    row_kwargs.update(
        {k: v for k, v in kwargs.items() if k in ("agent_id", "is_bot", "visibility")}
    )
    msg = await factories.make_agent_message(
        db_session, run=run, message_ts="1.0001", content="body", **row_kwargs
    )
    await db_session.flush()

    found = (await db_session.execute(
        select(AgentMessage.id).where(
            AgentMessage.simulation_run_id == run.id,
            gate_clause(gate),
        )
    )).scalars().all()
    sql_visible = msg.id in found

    entry_kwargs = dict(ts="1", channel="c", agent_id="x", name="X", content="")
    entry_kwargs.update(kwargs)
    python_visible = _entry_allowed(_post(**entry_kwargs), gate)

    assert sql_visible == expected, f"SQL disagreed with the table on: {name}"
    assert sql_visible == python_visible, (
        f"gate_clause and _entry_allowed disagree on: {name}"
    )


async def test_gate_is_the_union_of_co_members(db_session, monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(db_session, agent_id="spoke1", bot_name="Spoke1Bot")
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "pair1", "spoke1", "hub")
    await _cohort(db_session, "pair2", "spoke2", "hub")

    assert await resolve_agent_gate(db_session, "spoke1") == {"spoke1", "hub"}
    assert await resolve_agent_gate(db_session, "spoke2") == {"spoke2", "hub"}
    assert await resolve_agent_gate(db_session, "hub") == {"spoke1", "spoke2", "hub"}


async def test_uncohorted_agent_is_isolated_under_policy_isolated(
    db_session, monkeypatch
):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(db_session, agent_id="lonely", bot_name="LonelyBot")
    await factories.make_agent(db_session, agent_id="other", bot_name="OtherBot")
    await _cohort(db_session, "somepair", "other")

    assert await resolve_agent_gate(db_session, "lonely") == set()


async def test_gate_is_off_when_isolation_is_disabled(db_session, monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", False, raising=False)

    await factories.make_agent(db_session, agent_id="anyone", bot_name="AnyoneBot")

    assert await resolve_agent_gate(db_session, "anyone") is None


async def test_an_inactive_viewing_agent_still_resolves(db_session, monkeypatch):
    """compute_gates only keys the roster it is given, and the conversations route
    admits status 'inactive'. Without adding the viewer to the roster this raised
    KeyError instead of returning a gate."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(
        db_session, agent_id="sleeper", bot_name="SleeperBot", status="inactive"
    )
    await factories.make_agent(db_session, agent_id="awake", bot_name="AwakeBot")
    await _cohort(db_session, "mixed", "sleeper", "awake")

    assert await resolve_agent_gate(db_session, "sleeper") == {"sleeper", "awake"}
