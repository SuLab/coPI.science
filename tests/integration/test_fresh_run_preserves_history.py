"""`--fresh` opens a new run; it must not destroy every OTHER run's history.

`src/agent/main.py` used to answer `--fresh` with three UNFILTERED deletes:

    await db.execute(AgentMessage.__table__.delete())
    await db.execute(AgentChannel.__table__.delete())
    await db.execute(PiDmMessage.__table__.delete())

No `simulation_run_id` predicate anywhere — so a fresh start truncated the
conversation history of every run that had ever existed. Measured 2026-08-22:
`llm_call_logs` held 10 runs and `opportunity_assessments` 5, while
`agent_messages` held **1**; run 8b64a0e0's 1,354 messages were gone and 57 of
64 assessments pointed at a `slack_ts` that resolved to no message. The
assessment detail page's interview timeline was empty for 90% of the corpus.

The fix is "delete nothing" — a new `simulation_run_id` already isolates a
fresh run everywhere the engine reads. It is only safe with the second test
here: `_sync_private_channels_from_db` had NO run filter, so with the wipe gone
a fresh run would discover every previous run's `collab_private` channels, join
its bots to them, and (via `_seed_slack_cursors_without_ingest`) re-import
their whole Slack back catalogue.
"""
import pytest
from sqlalchemy import func, select

from src.agent.agent import Agent
from src.agent.main import _open_fresh_run
from src.agent.simulation import SimulationEngine
from src.models import AgentChannel, AgentMessage, SimulationRun
from src.visibility import VISIBILITY_COLLAB_PRIVATE
from tests import factories

pytestmark = pytest.mark.integration


class _FixtureSessionFactory:
    """Route a self-opened engine session at the rolled-back test session.

    Same shape as tests/integration/test_message_persistence.py's: the caller
    does ``async with self.session_factory() as db: ... await db.commit()``, the
    test session is in create_savepoint mode, and __aexit__ must NOT close the
    fixture-owned session.
    """

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_fresh_does_not_delete_another_runs_messages(db_session):
    """The data-destruction bug, pinned. Re-add any table-wide delete and this fails."""
    old_run = await factories.make_simulation_run(db_session)
    kept_message = await factories.make_agent_message(
        db_session, run=old_run, message_ts="1700000001.000100",
    )
    kept_channel = await factories.make_agent_channel(db_session, run=old_run)
    await db_session.flush()

    new_run_id = await _open_fresh_run(
        _FixtureSessionFactory(db_session), {"agent_count": 1},
    )

    assert new_run_id != old_run.id, "a fresh start must open its own run"
    surviving_messages = (await db_session.execute(
        select(AgentMessage.id).where(AgentMessage.simulation_run_id == old_run.id)
    )).scalars().all()
    assert surviving_messages == [kept_message.id], (
        "--fresh destroyed a previous run's agent_messages — the run is no "
        "longer auditable and its assessments' slack_ts resolve to nothing"
    )
    surviving_channels = (await db_session.execute(
        select(AgentChannel.id).where(AgentChannel.simulation_run_id == old_run.id)
    )).scalars().all()
    assert surviving_channels == [kept_channel.id], (
        "--fresh destroyed a previous run's agent_channels"
    )
    assert (await db_session.execute(
        select(func.count(SimulationRun.id)).where(SimulationRun.id == new_run_id)
    )).scalar_one() == 1


async def test_a_fresh_run_does_not_adopt_a_previous_runs_private_channel(db_session):
    """The HARD PREREQUISITE for "delete nothing".

    The fixture is deliberately a PREVIOUS run's `collab_private` channel: that
    is the row the unfiltered select used to hand a brand-new run.
    """
    old_run = await factories.make_simulation_run(db_session)
    stale = await factories.make_agent_channel(
        db_session, run=old_run,
        channel_name="priv-old-run", channel_id="G_old_run",
        visibility=VISIBILITY_COLLAB_PRIVATE,
    )
    await factories.make_private_channel_member(
        db_session, channel=stale, role="bot", agent_id="wang",
    )
    this_run = await factories.make_simulation_run(db_session)
    await db_session.flush()

    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={},
        session_factory=_FixtureSessionFactory(db_session),
        simulation_run_id=this_run.id,
    )

    await eng._sync_private_channels_from_db()

    assert "priv-old-run" not in eng._channel_id_map, (
        "a fresh run adopted a PREVIOUS run's private channel; its bots would "
        "join it and re-ingest its entire Slack back catalogue"
    )
    assert "priv-old-run" not in agent.state.subscribed_channels
    assert "G_old_run" not in eng._private_channel_members


async def test_this_runs_private_channel_is_still_adopted(db_session):
    """The other direction: the run filter must not disable discovery outright."""
    this_run = await factories.make_simulation_run(db_session)
    mine = await factories.make_agent_channel(
        db_session, run=this_run,
        channel_name="priv-this-run", channel_id="G_this_run",
        visibility=VISIBILITY_COLLAB_PRIVATE,
    )
    await factories.make_private_channel_member(
        db_session, channel=mine, role="bot", agent_id="wang",
    )
    await db_session.flush()

    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={},
        session_factory=_FixtureSessionFactory(db_session),
        simulation_run_id=this_run.id,
    )

    await eng._sync_private_channels_from_db()

    assert eng._channel_id_map.get("priv-this-run") == "G_this_run"
    assert "priv-this-run" in agent.state.subscribed_channels
    assert eng._private_channel_members.get("G_this_run") == {"wang"}
