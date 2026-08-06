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
from tests.integration.test_agent_page import _auth
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
    admits status 'inactive'. Without adding the viewer to the roster, 'sleeper'
    would be absent from compute_gates' agent_ids, so gates.get('sleeper') would
    silently return None (gate off / unrestricted) instead of the viewer's real
    cohort gate {'sleeper', 'awake'} — the opposite of the intended isolation,
    and worse than a KeyError because it fails open rather than loud."""
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


async def test_a_spoke_pi_does_not_see_another_spokes_bot(
    client, db_session, monkeypatch
):
    """The star topology: two spokes and a hub. Spoke 1's PI must not see
    Spoke 2's bot, and MUST still see the hub (the positive control)."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi1 = await factories.make_user(db_session, name="Spoke One", email="s1@example.org")
    await factories.make_agent(
        db_session, user=pi1, agent_id="spoke1", bot_name="Spoke1Bot", pi_name="Spoke One"
    )
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "pair1", "spoke1", "hub")
    await _cohort(db_session, "pair2", "spoke2", "hub")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    # Spoke 1's own post is what puts #general in its channel set.
    await factories.make_agent_message(
        db_session, agent_id="spoke1", message_ts="1.0001",
        content="MINE-own-post", sender_name="Spoke1Bot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="hub", message_ts="1.0002",
        content="HUB-visible-post", sender_name="HubBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="spoke2", message_ts="1.0003",
        content="LEAK-other-spoke-post", sender_name="Spoke2Bot", **common
    )
    await db_session.commit()

    page = await client.get("/agent/spoke1/conversations", headers=_auth(pi1.id))
    assert page.status_code == 200
    assert "MINE-own-post" in page.text
    assert "HUB-visible-post" in page.text, "positive control: the hub must be visible"
    assert "LEAK-other-spoke-post" not in page.text
    assert "Spoke2Bot" not in page.text


async def test_a_pi_message_still_renders_under_the_gate(
    client, db_session, monkeypatch
):
    """is_bot=False bypasses the gate — the human bypass must survive."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Solo PI", email="solo@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="solo", bot_name="SoloBot", pi_name="Solo PI"
    )
    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="solo", message_ts="2.0001",
        content="BOT-anchor", sender_name="SoloBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id=None, is_bot=False, message_ts="2.0002",
        content="HUMAN-said-this", sender_name="Solo PI (PI)", **common
    )
    await db_session.commit()

    page = await client.get("/agent/solo/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "HUMAN-said-this" in page.text


async def test_replies_are_not_listed_as_top_level_rows(
    client, db_session, monkeypatch
):
    """The feed selects ROOTS. A reply appears via its count, not as its own card."""
    from src.config import get_settings
    monkeypatch.setattr(
        get_settings(), "cohort_isolation_enabled", False, raising=False
    )

    pi = await factories.make_user(db_session, name="Root PI", email="root@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="rooter", bot_name="RooterBot", pi_name="Root PI"
    )
    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="rooter", message_ts="3.0001", phase="new_post",
        content="THE-ROOT", sender_name="RooterBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="rooter", message_ts="3.0002", thread_ts="3.0001",
        phase="thread_reply", content="THE-REPLY", sender_name="RooterBot", **common
    )
    await db_session.commit()

    page = await client.get("/agent/rooter/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "THE-ROOT" in page.text
    assert "THE-REPLY" not in page.text, "a reply must not render as a top-level card"


async def test_a_delegate_sees_exactly_what_the_owner_sees(
    client, db_session, monkeypatch
):
    """Access is owner-or-delegate; the gate is the AGENT's, not the viewer's, so
    both must get byte-identical feeds."""
    from src.config import get_settings
    from src.models import AgentDelegate
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Owner", email="own@example.org")
    agent = await factories.make_agent(
        db_session, user=pi, agent_id="deleg", bot_name="DelegBot", pi_name="Owner"
    )
    await factories.make_agent(db_session, agent_id="stranger", bot_name="StrangerBot")
    await _cohort(db_session, "solo", "deleg")

    dee = await factories.make_user(db_session, name="Dee", email="dee2@example.org")
    db_session.add(AgentDelegate(agent_registry_id=agent.id, user_id=dee.id))

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="deleg", message_ts="4.0001",
        content="OWN-POST", sender_name="DelegBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="stranger", message_ts="4.0002",
        content="OUTSIDER-POST", sender_name="StrangerBot", **common
    )
    await db_session.commit()

    owner_page = await client.get("/agent/deleg/conversations", headers=_auth(pi.id))
    dee_page = await client.get("/agent/deleg/conversations", headers=_auth(dee.id))
    assert owner_page.status_code == 200
    assert dee_page.status_code == 200
    assert "OWN-POST" in dee_page.text
    assert "OUTSIDER-POST" not in owner_page.text
    assert "OUTSIDER-POST" not in dee_page.text
