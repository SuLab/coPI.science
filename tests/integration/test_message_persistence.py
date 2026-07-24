"""Integration tests for the DB-primary message persistence guards (PR #19 review).

Exercised against the real migrated Postgres so the actual ON CONFLICT upsert
(including its M1a human-row guard) is validated, not just the Python logic.
See specs/local-db-conversations.md.
"""

import time

import pytest
from sqlalchemy import func, select

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import (
    PI_INBOX_LOOKBACK_S,
    REBUILD_WINDOW_S,
    SimulationEngine,
)
from src.models import AgentMessage, PiDmMessage
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


def _engine_for(session, run_id, agents=None):
    return SimulationEngine(
        agents=agents or [], slack_clients={},
        session_factory=_FixtureSessionFactory(session),
        simulation_run_id=run_id,
    )


class _RecordingPiHandler:
    """Minimal PIHandler stand-in that records handle_dm calls."""

    def __init__(self):
        self.calls = []

    async def handle_dm(self, agent_id, pi_user_id, content):
        self.calls.append((agent_id, pi_user_id, content))


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


# ---------------------------------------------------------------
# H2 — the inbox pollers must not skip a row that committed below the cursor
# (posted_at is stamped at creation, so a late-committing PI row lands below a
# cursor already advanced past its timestamp).
# ---------------------------------------------------------------

async def test_inbound_poller_ingests_pi_row_committed_below_cursor(db_session):
    run = await factories.make_simulation_run(db_session)
    engine = _engine_for(db_session, run.id)
    # The cursor has already advanced (engine flushed its own later message).
    engine._pi_inbox_cursor = 1700000200.0
    # A PI row whose creation-time posted_at is *below* the cursor but within the
    # lookback window — the H2 race. The old `posted_at > cursor` filter skipped
    # it forever.
    below_ts = "1700000150.000000"
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=below_ts, posted_at=float(below_ts),
        content="late-committed PI message", sender_name="PI",
    )
    await engine._poll_inbound_from_db()

    entry = engine.message_log.get_entry(below_ts)
    assert entry is not None
    assert entry.content == "late-committed PI message"


async def test_inbound_poller_skips_row_older_than_lookback(db_session):
    # Bounds the re-scan: a row far below the lookback floor is not re-queried.
    run = await factories.make_simulation_run(db_session)
    engine = _engine_for(db_session, run.id)
    engine._pi_inbox_cursor = 1700000200.0
    ancient_ts = f"{1700000200.0 - PI_INBOX_LOOKBACK_S - 100:.6f}"
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=ancient_ts, posted_at=float(ancient_ts),
        content="ancient", sender_name="PI",
    )
    await engine._poll_inbound_from_db()
    assert engine.message_log.get_entry(ancient_ts) is None


async def test_dm_poller_ingests_below_cursor_then_dedups(db_session):
    run = await factories.make_simulation_run(db_session)
    agent = Agent("su", "SuBot", "Andrew Su")
    engine = _engine_for(db_session, run.id, agents=[agent])
    handler = _RecordingPiHandler()
    engine._pi_handler = handler
    engine._pi_dm_cursor = 1700000200.0

    below_ts = "1700000150.000000"
    db_session.add(PiDmMessage(
        simulation_run_id=run.id, agent_id="su", pi_user_id="local:x",
        direction="inbound", content="standing instruction",
        sender_name="PI", ts=below_ts, posted_at=float(below_ts),
    ))
    await db_session.flush()

    # First poll ingests the below-cursor row (H2)...
    await engine._poll_pi_dms_from_db()
    assert handler.calls == [("su", "local:x", "standing instruction")]

    # ...and the lookback re-scan on the next poll does NOT re-process it.
    await engine._poll_pi_dms_from_db()
    assert len(handler.calls) == 1


async def test_seed_pi_dm_cursor_prevents_replay_on_restart(db_session):
    # Seeding the seen-set (not just the cursor) means the first poll's lookback
    # re-scan doesn't replay recent history through handle_dm after a restart.
    run = await factories.make_simulation_run(db_session)
    ts = "1700000150.000000"
    db_session.add(PiDmMessage(
        simulation_run_id=run.id, agent_id="su", pi_user_id="local:x",
        direction="inbound", content="old directive",
        sender_name="PI", ts=ts, posted_at=float(ts),
    ))
    await db_session.flush()

    agent = Agent("su", "SuBot", "Andrew Su")
    engine = _engine_for(db_session, run.id, agents=[agent])
    handler = _RecordingPiHandler()
    engine._pi_handler = handler

    await engine._seed_pi_dm_cursor()
    assert ts in engine._pi_dm_seen
    await engine._poll_pi_dms_from_db()
    assert handler.calls == []


# ---------------------------------------------------------------
# B1 — the cosmetic run-stats COUNT is throttled, not run every flush.
# ---------------------------------------------------------------

async def _count_messages(session, run_id):
    return (await session.execute(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.simulation_run_id == run_id
        )
    )).scalar_one()


async def test_flush_throttles_run_stats_count(db_session):
    run = await factories.make_simulation_run(db_session)
    engine = _engine_for(db_session, run.id)

    def _enqueue(ts, content):
        engine._pending_persist = [LogEntry(
            ts=ts, channel="general", sender_agent_id="su",
            sender_name="SuBot", content=content, posted_at=float(ts), is_bot=True,
        )]

    # First flush refreshes the counter.
    _enqueue("100000001.000000", "a")
    await engine._flush_persisted()
    assert run.total_messages == 1

    # Second flush within the interval inserts a row but does NOT recount — the
    # counter is intentionally stale (throttled) even though 2 rows now exist.
    _enqueue("100000002.000000", "b")
    await engine._flush_persisted()
    assert await _count_messages(db_session, run.id) == 2
    assert run.total_messages == 1

    # force_stats (used on shutdown) recomputes immediately.
    _enqueue("100000003.000000", "c")
    await engine._flush_persisted(force_stats=True)
    assert run.total_messages == 3


# ---------------------------------------------------------------
# B2 — the startup rebuild loads a bounded window (recent + undecided threads),
# and old closed threads are hydrated on demand.
# ---------------------------------------------------------------

async def test_rebuild_windows_recent_and_undecided_only(db_session):
    run = await factories.make_simulation_run(db_session)
    old = time.time() - REBUILD_WINDOW_S - 100_000
    now = time.time()

    # Old + closed (has a ThreadDecision) → windowed out.
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="OLDCLOSED", thread_ts=None, posted_at=old, content="old closed root",
    )
    await factories.make_thread_decision(db_session, run=run, thread_id="OLDCLOSED")

    # Old + undecided (no ThreadDecision) → loaded in full.
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="OLDLIVE", thread_ts=None, posted_at=old, content="old live root",
    )
    # Recent → loaded.
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="RECENT", thread_ts=None, posted_at=now, content="recent",
    )

    engine = _engine_for(db_session, run.id)
    await engine._rebuild_state_from_db()

    assert engine.message_log.get_entry("OLDCLOSED") is None
    assert engine.message_log.get_entry("OLDLIVE") is not None
    assert engine.message_log.get_entry("RECENT") is not None


async def test_hydrate_thread_loads_windowed_out_thread(db_session):
    run = await factories.make_simulation_run(db_session)
    old = time.time() - REBUILD_WINDOW_S - 100_000
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="THR", thread_ts=None, posted_at=old, content="root",
    )
    await factories.make_agent_message(
        db_session, run=run, agent_id="lairson", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="THR-r1", thread_ts="THR", posted_at=old + 1, content="reply",
    )

    engine = _engine_for(db_session, run.id)
    assert engine.message_log.get_entry("THR") is None  # not yet loaded

    await engine._hydrate_thread_from_db("THR")
    assert engine.message_log.get_entry("THR") is not None
    assert len(engine.message_log.get_thread_history("THR")) == 2

    # Idempotent — a second hydrate doesn't duplicate.
    await engine._hydrate_thread_from_db("THR")
    assert len(engine.message_log.get_thread_history("THR")) == 2
