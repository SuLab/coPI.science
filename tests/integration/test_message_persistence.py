"""Integration tests for the DB-primary message persistence guards (PR #19 review).

Exercised against the real migrated Postgres so the actual ON CONFLICT upsert
(including its M1a human-row guard) is validated, not just the Python logic.
See specs/local-db-conversations.md.
"""

import pytest
from sqlalchemy import select

from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.models import AgentMessage
from tests import factories

pytestmark = pytest.mark.integration


class _FixtureSessionFactory:
    """Route the engine's self-opened session at the rolled-back test session.

    _flush_persisted does ``async with self.session_factory() as db: ... await
    db.commit()``. The test session runs in create_savepoint mode, so commit()
    just releases a savepoint and the outer transaction still rolls back at
    teardown. __aexit__ must NOT close the fixture-owned session.
    """

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


def _engine_for(session, run_id):
    return SimulationEngine(
        agents=[], slack_clients={},
        session_factory=_FixtureSessionFactory(session),
        simulation_run_id=run_id,
    )


async def test_flush_upsert_does_not_clobber_human_row_with_bot(db_session):
    # M1a: a cross-process canonical-id collision must not let a bot message
    # overwrite an existing human (PI) row in the now-authoritative store.
    run = await factories.make_simulation_run(db_session)
    collide_ts = "1700000000.123456"
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=collide_ts, posted_at=float(collide_ts),
        content="HUMAN PI MESSAGE", sender_name="Dr Human (PI)",
    )

    engine = _engine_for(db_session, run.id)
    engine._pending_persist = [LogEntry(
        ts=collide_ts, channel="general", sender_agent_id="subot",
        sender_name="SuBot", content="BOT CLOBBER ATTEMPT",
        posted_at=float(collide_ts), is_bot=True,
    )]
    await engine._flush_persisted()

    row = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
        AgentMessage.message_ts == collide_ts,
    ))).scalar_one()
    assert row.is_bot is False
    assert row.agent_id is None
    assert row.content == "HUMAN PI MESSAGE"


async def test_flush_upsert_still_updates_own_bot_row(db_session):
    # The guard must not break the legitimate idempotent re-flush / slack-mirror
    # path: a bot row re-flushed at the same ts updates in place.
    run = await factories.make_simulation_run(db_session)
    bot_ts = "1700000000.222222"
    engine = _engine_for(db_session, run.id)
    for text in ("v1", "v2-updated"):
        engine._pending_persist = [LogEntry(
            ts=bot_ts, channel="general", sender_agent_id="subot",
            sender_name="SuBot", content=text,
            posted_at=float(bot_ts), is_bot=True,
        )]
        await engine._flush_persisted()

    row = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
        AgentMessage.message_ts == bot_ts,
    ))).scalar_one()
    assert row.content == "v2-updated"


async def test_flush_upsert_allows_human_reflush(db_session):
    # An ingested human PI message re-flushed by the engine (is_bot=False both
    # sides) must still update — the guard only blocks bot-over-human.
    run = await factories.make_simulation_run(db_session)
    ts = "1700000000.333333"
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=ts, posted_at=float(ts),
        content="original", sender_name="PI",
    )
    engine = _engine_for(db_session, run.id)
    engine._pending_persist = [LogEntry(
        ts=ts, channel="general", sender_agent_id=None,
        sender_name="PI", content="edited", posted_at=float(ts), is_bot=False,
    )]
    await engine._flush_persisted()

    row = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
        AgentMessage.message_ts == ts,
    ))).scalar_one()
    assert row.content == "edited"
    assert row.is_bot is False
