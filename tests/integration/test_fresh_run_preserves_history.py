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
fresh run everywhere the engine reads.
"""
import pytest
from sqlalchemy import func, select

from src.agent.main import _open_fresh_run
from src.models import AgentChannel, AgentMessage, PiDmMessage, SimulationRun
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
    # No factory for this one — it is a dead table with no writer in `src/`.
    db_session.add(PiDmMessage(
        simulation_run_id=old_run.id, agent_id="agent1", pi_user_id="U_pi",
        direction="outbound", content="a DM from a previous run",
        ts="1700000001.000200",
    ))
    await db_session.flush()
    kept_dms = (await db_session.execute(
        select(func.count(PiDmMessage.id))
    )).scalar_one()
    assert kept_dms >= 1, "setup: the pi_dm_messages fixture did not land"

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
    # The THIRD delete. `pi_dm_messages` is a dead table — nothing in `src/`
    # writes it and prod holds 0 rows — so re-adding its delete alone leaves the
    # two assertions above green and 131 integration tests green with it. It was
    # one of the three unfiltered deletes the CRITICAL item was about, so it gets
    # its own row and its own assertion rather than resting on the other two.
    assert (await db_session.execute(
        select(func.count(PiDmMessage.id))
    )).scalar_one() == kept_dms, (
        "--fresh truncated pi_dm_messages — a dead table today, but the delete "
        "was table-wide and unfiltered like the other two"
    )
    assert (await db_session.execute(
        select(func.count(SimulationRun.id)).where(SimulationRun.id == new_run_id)
    )).scalar_one() == 1

