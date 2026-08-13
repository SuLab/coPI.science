"""Integration tests for the DB-primary message persistence guards (PR #19 review).

Exercised against the real migrated Postgres so the actual ON CONFLICT upsert
(including its M1a human-row guard) is validated, not just the Python logic.
See specs/local-db-conversations.md.
"""

import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.agent.message_log import LogEntry
from src.agent.simulation import (
    PI_INBOX_LOOKBACK_S,
    REBUILD_WINDOW_S,
    SimulationEngine,
)
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


def _engine_for(session, run_id, agents=None):
    return SimulationEngine(
        agents=agents or [], slack_clients={},
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


# ---------------------------------------------------------------
# R1 — the collision the M1a guard resolves lossily must not be reachable in the
# first place: concurrent writers mint into disjoint slots, so both messages
# survive against the real unique constraint.
# ---------------------------------------------------------------

async def test_concurrent_writers_both_persist_at_the_same_instant(db_session, monkeypatch):
    import time as time_mod

    from src.agent.ids import (
        WRITER_ENGINE,
        WRITER_WEB,
        TsMinter,
        set_default_writer_id,
    )
    from src.services.pi_inbox import record_pi_message

    run = await factories.make_simulation_run(db_session)

    # Freeze the clock: every mint in this test sees the identical microsecond,
    # which is exactly the case that used to yield one id and drop a message.
    monkeypatch.setattr(time_mod, "time_ns", lambda: 1_800_000_000_000_000_000)

    engine = _engine_for(db_session, run.id)
    engine._ts_minter = TsMinter(WRITER_ENGINE)
    set_default_writer_id(WRITER_WEB)

    bot_ts = engine.mint_ts()
    engine._pending_persist = [LogEntry(
        ts=bot_ts, channel="general", sender_agent_id="subot",
        sender_name="SuBot", content="BOT MESSAGE",
        posted_at=float(bot_ts), is_bot=True,
    )]
    await engine._flush_persisted()

    # The web app's writer, minting at the same frozen instant.
    pi_msg = await record_pi_message(
        db_session, run_id=run.id, channel_name="general",
        content="PI: please pivot to aging biology", sender_name="Dr Human (PI)",
    )
    await db_session.flush()

    assert pi_msg.message_ts != bot_ts
    rows = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
    ))).scalars().all()
    contents = {r.content for r in rows}
    assert contents == {"BOT MESSAGE", "PI: please pivot to aging biology"}


# ---------------------------------------------------------------
# H2 — the inbox pollers must not skip a row that committed below the cursor
# (the stamp is written at row creation, so a late-committing PI row lands below
# a cursor already advanced past it).
# ---------------------------------------------------------------

async def test_inbound_poller_ingests_pi_row_committed_below_cursor(db_session):
    run = await factories.make_simulation_run(db_session)
    engine = _engine_for(db_session, run.id)
    # A PI row whose stamp is *below* the cursor but within the lookback window —
    # the H2 race. The old `> cursor` filter skipped it forever.
    below_ts = "1700000150.000000"
    row = await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=below_ts, posted_at=float(below_ts),
        content="late-committed PI message", sender_name="PI",
    )
    await db_session.refresh(row)
    # The cursor has already advanced (engine flushed its own later message).
    # Derived from the row's own created_at so the assertion doesn't depend on
    # this process's clock matching the DB server's — the point of R3.
    engine._pi_inbox_cursor = row.created_at + timedelta(seconds=50)

    await engine._poll_inbound_from_db()

    entry = engine.message_log.get_entry(below_ts)
    assert entry is not None
    assert entry.content == "late-committed PI message"


async def test_inbound_poller_skips_row_older_than_lookback(db_session):
    # Bounds the re-scan: a row far below the lookback floor is not re-queried.
    run = await factories.make_simulation_run(db_session)
    engine = _engine_for(db_session, run.id)
    ancient_ts = "1700000000.000000"
    row = await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=ancient_ts, posted_at=float(ancient_ts),
        content="ancient", sender_name="PI",
    )
    await db_session.refresh(row)
    engine._pi_inbox_cursor = row.created_at + timedelta(
        seconds=PI_INBOX_LOOKBACK_S + 100
    )
    await engine._poll_inbound_from_db()
    assert engine.message_log.get_entry(ancient_ts) is None


async def test_inbound_poller_delivers_a_row_from_a_skewed_writer_clock(db_session):
    # R3: a writer whose clock is far behind the engine's stamps posted_at well
    # below the cursor. Paging over created_at (the DB server's clock) delivers
    # it anyway; the old posted_at cursor dropped it silently and forever.
    run = await factories.make_simulation_run(db_session)
    engine = _engine_for(db_session, run.id)

    recent = await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="1700009000.000000", posted_at=1700009000.0,
        content="engine post", sender_name="SuBot",
    )
    await db_session.refresh(recent)
    engine._pi_inbox_cursor = recent.created_at

    skewed_ts = "1600000000.000000"  # ~3 years of clock skew
    skewed = await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="local:general", channel_name="general",
        message_ts=skewed_ts, posted_at=float(skewed_ts),
        content="PI message from a skewed host", sender_name="PI",
    )
    await db_session.refresh(skewed)
    assert skewed.posted_at < engine._pi_inbox_cursor.timestamp() - PI_INBOX_LOOKBACK_S

    await engine._poll_inbound_from_db()

    entry = engine.message_log.get_entry(skewed_ts)
    assert entry is not None
    assert entry.content == "PI message from a skewed host"


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


# ---------------------------------------------------------------
# The Slack mirror mapping has to survive a restart, otherwise the engine
# cannot tell a Slack-backed thread from a DB-origin one and would mirror
# replies against an id Slack has never seen.
# ---------------------------------------------------------------

async def test_rebuild_restores_the_slack_mapping(db_session):
    run = await factories.make_simulation_run(db_session)
    now = time.time()
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="C0SLACK", channel_name="general",
        message_ts="MIRRORED", posted_at=now, content="db-origin, then mirrored",
        slack_ts="1700009999.111111", slack_channel_id="C0SLACK",
    )
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="DBONLY", posted_at=now, content="never mirrored",
    )

    engine = _engine_for(db_session, run.id)
    await engine._rebuild_state_from_db()

    assert engine.message_log.get_entry("MIRRORED").slack_ts == "1700009999.111111"
    assert engine._slack_parent_ts("MIRRORED") == "1700009999.111111"
    # A DB-origin root has no Slack presence — replies to it must not be mirrored.
    assert engine.message_log.get_entry("DBONLY").slack_ts is None
    assert engine._slack_parent_ts("DBONLY") is None


async def test_rebuild_never_infers_a_slack_ts_from_the_channel_id(db_session):
    """A NULL slack_ts means "not on Slack", even in a real Slack channel.

    The rebuild used to infer the mapping for such a row, on the theory that it
    predated Stage 6. But a DB-origin message can carry a real Slack channel id
    too — a PI message written through the web inbox resolves channel_id from the
    agent_channels row, and so does an agent post whose mirror failed. Inferring
    hands _slack_parent_ts a canonical id Slack never issued, which then goes out
    as a chat.postMessage thread_ts and orphans the reply. Legacy rows are
    repaired by scripts/backfill_slack_ts.py, which asks Slack instead of guessing.
    """
    run = await factories.make_simulation_run(db_session)
    # Exactly the shape that used to be mis-inferred: web-written PI message,
    # locally-minted canonical id, stored against the channel's real Slack id.
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=False,
        channel_id="C0SLACK", channel_name="general",
        message_ts="1800000000.000001", posted_at=1800000000.000001,
        content="@SuBot a PI message written from the web", slack_ts=None,
        sender_name="Dr Human (PI)",
    )

    engine = _engine_for(db_session, run.id)
    await engine._rebuild_state_from_db()

    assert engine.message_log.get_entry("1800000000.000001").slack_ts is None
    assert engine._slack_parent_ts("1800000000.000001") is None


# ---------------------------------------------------------------
# R1 (residual) — every canonical id must come from the shared minter, so it
# carries its process's writer slot. The Slack-off private-channel handover was
# the last site formatting ids straight off time.time().
# ---------------------------------------------------------------


async def test_offline_migration_mints_ids_in_its_own_writer_slot(db_session, monkeypatch):
    import time as time_mod

    from src.agent.ids import (
        WRITER_ENGINE,
        WRITER_SLOT_MODULUS,
        WRITER_WEB,
        TsMinter,
        set_default_writer_id,
    )
    from src.services.private_channels import _migrate_offline

    run = await factories.make_simulation_run(db_session)
    pi_user = await factories.make_user(db_session)
    td = await factories.make_thread_decision(
        db_session, run=run, agent_a="su", agent_b="wiseman",
        channel="general", summary_text="A joint proposal.",
    )

    # Freeze BOTH clocks the two id schemes read (time_ns for the minter,
    # time for the old hand-rolled format), so the engine and the migration mint
    # at the identical microsecond — the case that used to collide.
    monkeypatch.setattr(time_mod, "time_ns", lambda: 1_800_000_000_000_000_000)
    monkeypatch.setattr(time_mod, "time", lambda: 1_800_000_000.0)

    engine = _engine_for(db_session, run.id)
    engine._ts_minter = TsMinter(WRITER_ENGINE)
    set_default_writer_id(WRITER_WEB)

    bot_ts = engine.mint_ts()
    engine._pending_persist = [LogEntry(
        ts=bot_ts, channel="general", sender_agent_id="su",
        sender_name="SuBot", content="BOT MESSAGE",
        posted_at=float(bot_ts), is_bot=True,
    )]
    await engine._flush_persisted()

    # The web process writes the handover at that same frozen instant. Under the
    # old scheme its first id was f"{time.time():.6f}" == the engine's id, so the
    # ORM insert below hit uq_agent_messages_run_ts.
    await _migrate_offline(
        db_session,
        thread_decision=td,
        creator_agent_id="su",
        creator_pi_user=pi_user,
        guidance_text="Narrow the aim to one assay.",
        a="su", b="wiseman",
        other_agent_id="wiseman",
        origin_channel_name="general",
    )
    await db_session.flush()

    rows = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
    ))).scalars().all()
    assert "BOT MESSAGE" in {r.content for r in rows}

    handover = [r for r in rows if r.message_ts != bot_ts]
    # 2+ handover posts in the new private channel, plus the origin-thread marker.
    assert len(handover) >= 3
    assert any(r.thread_ts == td.thread_id for r in handover)

    # Every handover id sits in the web writer's residue class, so it can never
    # coincide with an engine- or GrantBot-minted id ...
    for r in handover:
        assert int(r.message_ts.partition(".")[2]) % WRITER_SLOT_MODULUS == WRITER_WEB
    # ... and they stay distinct and float-ordered (posted_at == float(ts)).
    minted = sorted(r.message_ts for r in handover)
    assert len(set(minted)) == len(minted)
    floats = [float(t) for t in minted]
    assert all(b > a for a, b in zip(floats, floats[1:], strict=False))
    assert all(r.posted_at == float(r.message_ts) for r in handover)


# ---------------------------------------------------------------
# The Slack-*on* migration used to post the handover to Slack without recording
# it in agent_messages — the last place a message existed on Slack before it
# existed in the primary store.
# ---------------------------------------------------------------


def _patch_slack_migration(monkeypatch, clients: dict):
    """Route private_channels' Slack surface at FakeSlackClient instances."""
    from src.services import private_channels as pc
    from tests.fakes import FakeSlackClient

    async def _enabled(*args, **kwargs):
        return True

    async def _token(db, agent_id):
        return f"xoxb-fake-{agent_id}"

    async def _other_pi(db, agent_id):
        return None, None  # no claimed PI on the other side — skips the DM branch

    def _client(agent_id, token):
        return clients.setdefault(agent_id, FakeSlackClient(agent_id=agent_id))

    monkeypatch.setattr(pc, "_slack_enabled_for_migration", _enabled)
    monkeypatch.setattr(pc, "_get_or_fail_bot_token", _token)
    monkeypatch.setattr(pc, "_resolve_other_pi", _other_pi)
    monkeypatch.setattr(pc, "_make_client", _client)
    return pc


async def test_slack_migration_mirrors_the_handover_into_the_db(db_session, monkeypatch):
    clients: dict = {}
    pc = _patch_slack_migration(monkeypatch, clients)

    run = await factories.make_simulation_run(db_session)
    pi_user = await factories.make_user(db_session)
    # A Slack-born origin root: stored against a real Slack channel, so its
    # canonical id is also its Slack ts.
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="C0ORIGIN", channel_name="general",
        message_ts="1700000000.000500", posted_at=1700000000.0005,
        content="origin root", slack_ts="1700000000.000500",
    )
    td = await factories.make_thread_decision(
        db_session, run=run, agent_a="su", agent_b="wiseman",
        channel="general", thread_id="1700000000.000500",
        summary_text="A joint proposal.",
    )

    result = await pc.migrate_public_thread_to_private(
        db_session, thread_decision=td, creator_agent_id="su",
        creator_pi_user=pi_user, guidance_text="Narrow the aim to one assay.",
    )
    await db_session.flush()

    rows = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
        AgentMessage.content != "origin root",
    ))).scalars().all()

    # Sorted by canonical id, which is post order here (the fake ts increments).
    private_rows = sorted(
        (r for r in rows if r.channel_name == result.channel_name),
        key=lambda r: r.message_ts,
    )
    close_rows = [r for r in rows if r.channel_name == "general"]
    assert len(private_rows) >= 2      # the handover posts
    assert len(close_rows) == 1        # the origin-thread close marker

    # Slack-on parity (design rule 1): the canonical id IS the Slack ts, and the
    # mirror mapping is recorded so a later reconcile dedups instead of duplicating.
    posted_ts = {p["ts"] for p in clients["su"].posted}
    for r in private_rows + close_rows:
        assert r.slack_ts == r.message_ts
        assert r.message_ts in posted_ts
        assert r.posted_at == float(r.message_ts)
        assert r.is_bot is True
        assert r.sender_name == "suBot"
    assert all(r.visibility == "collab_private" for r in private_rows)
    # Stored content is the handover text itself (pre-mrkdwn), not a placeholder.
    expected = pc._build_handover_messages(
        creator_pi_name=pi_user.name,
        proposal_summary="A joint proposal.",
        guidance_text="Narrow the aim to one assay.",
        origin_channel_name="general",
    )
    assert [r.content for r in private_rows] == expected
    assert any("one assay" in r.content for r in private_rows)

    # The close marker threads on the root's Slack ts, in the origin channel, and
    # carries no PI guidance text.
    marker = close_rows[0]
    assert marker.visibility == "public"
    assert marker.thread_ts == "1700000000.000500"
    assert marker.slack_thread_ts == "1700000000.000500"
    assert "one assay" not in marker.content
    # ... and that is what Slack was actually asked to thread on.
    threaded = [p for p in clients["su"].posted if p["thread_ts"]]
    assert [p["thread_ts"] for p in threaded] == ["1700000000.000500"]


async def test_slack_migration_keeps_the_close_marker_db_only_for_a_db_origin_root(
    db_session, monkeypatch,
):
    """A thread started Slack-off has a minted root id Slack has never seen.

    The marker must not be posted against it (that detaches or errors), but it
    still has to land in the DB — the store the simulation actually reads.
    """
    clients: dict = {}
    pc = _patch_slack_migration(monkeypatch, clients)

    run = await factories.make_simulation_run(db_session)
    pi_user = await factories.make_user(db_session)
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", is_bot=True,
        channel_id="local:general", channel_name="general",
        message_ts="1800000000.000100", posted_at=1800000000.0001,
        content="db-origin root", slack_ts=None,
    )
    td = await factories.make_thread_decision(
        db_session, run=run, agent_a="su", agent_b="wiseman",
        channel="general", thread_id="1800000000.000100",
    )

    await pc.migrate_public_thread_to_private(
        db_session, thread_decision=td, creator_agent_id="su",
        creator_pi_user=pi_user, guidance_text="Keep going.",
    )
    await db_session.flush()

    # Nothing was posted into a thread on Slack ...
    assert [p for p in clients["su"].posted if p["thread_ts"]] == []
    # ... but the marker exists in the DB, unmirrored, on the canonical thread.
    marker = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
        AgentMessage.thread_ts == "1800000000.000100",
    ))).scalars().one()
    assert marker.slack_ts is None
    assert marker.slack_thread_ts is None
    assert marker.channel_name == "general"


# ---------------------------------------------------------------
# The channel poller's bot branch dropped the Slack mirror mapping, so a thread
# rooted at a polled bot post (GrantBot's funding posts) looked DB-origin and
# every reply to it was kept off Slack.
# ---------------------------------------------------------------


class _HistoryClient:
    """Connected transport that returns one canned bot message from history."""

    def __init__(self, messages):
        self.agent_id = "su"
        self._messages = messages

    @property
    def is_connected(self):
        return True

    def is_bot_user(self, user_id):
        return False

    def poll_channel_messages(self, channel_id, oldest="0", limit=100):
        return list(self._messages)

    def resolve_user_name(self, user_id):
        return user_id


async def test_polled_bot_message_keeps_its_slack_mapping(db_session):
    run = await factories.make_simulation_run(db_session)
    client = _HistoryClient([{
        "ts": "1700000123.456789",
        "bot_id": "B0DIGEST",
        "username": "DigestBot",
        "text": "Workspace digest: 3 new posts this week",
    }])

    engine = _engine_for(db_session, run.id)
    engine.slack_clients = {"su": client}
    engine._channel_id_map = {"general": "C0GENERAL"}
    engine._channel_visibility = {"general": "public"}
    # start() registers this; the poller's append has to reach the DB buffer.
    engine.message_log.set_persist_callback(engine._enqueue_persist)

    await engine._poll_slack_for_human_messages()

    entry = engine.message_log.get_entry("1700000123.456789")
    assert entry is not None
    # The mapping is what makes a reply mirrorable: without it _slack_parent_ts
    # reports "no Slack root" and _post_message keeps the reply DB-only.
    assert entry.slack_ts == "1700000123.456789"
    assert entry.slack_channel_id == "C0GENERAL"
    assert engine._slack_parent_ts("1700000123.456789") == "1700000123.456789"

    # And it survives the flush into the primary store.
    await engine._flush_persisted()
    row = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.simulation_run_id == run.id,
        AgentMessage.message_ts == "1700000123.456789",
    ))).scalars().one()
    assert row.slack_ts == "1700000123.456789"
    assert row.slack_channel_id == "C0GENERAL"
    assert row.is_bot is True
