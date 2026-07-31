"""Restart, reconcile, Slack-off<->Slack-on transitions, and the error paths.

T6, T10 and T11 of .notes/slack-integration-test-plan.md.

The hybrid state is the one production is actually in: a run that started with Slack off
has DB-origin roots Slack has never seen, and then Slack comes on. Nothing tested that
combination, and it is where `_slack_parent_ts` earns its keep.
"""

import time
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.transport import NullTransport
from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    SimulationRun,
)
from src.visibility import VISIBILITY_PUBLIC

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]

AGENTS = ("su", "cravatt", "wiseman")
POST_GAP = 1.1


@pytest.fixture
async def lifecycle(engine, slack_clients, slack_probe_channel, monkeypatch):
    """A factory that can build engines repeatedly over ONE simulation_run_id, so a
    restart is a genuinely new engine object against the same durable state."""
    import src.agent.simulation as sim

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    name, cid = slack_probe_channel
    monkeypatch.setattr(sim, "SEEDED_CHANNELS", [name])

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        for aid in AGENTS:
            db.add(AgentRegistry(agent_id=aid, bot_name=f"{aid.capitalize()}ProbeBot",
                                 pi_name=f"PI {aid}", status="active"))
        await db.commit()

    def build(*, slack_on: bool):
        agents = [Agent(agent_id=a, bot_name=f"{a.capitalize()}ProbeBot",
                        pi_name=f"PI {a}") for a in AGENTS]
        clients = (dict(slack_clients) if slack_on
                   else {a: NullTransport(a) for a in AGENTS})
        eng = SimulationEngine(
            agents=agents, slack_clients=clients, budget_cap=0,
            session_factory=factory, simulation_run_id=run_id, slack_enabled=slack_on,
        )
        eng.message_log.set_bot_name_map({f"{a}probebot": a for a in AGENTS})
        eng._bot_name_to_id = {f"{a}probebot": a for a in AGENTS}
        eng.message_log.set_persist_callback(eng._enqueue_persist)
        eng._channel_id_map = {name: cid if slack_on else f"local:{name}"}
        eng._channel_visibility = {name: VISIBILITY_PUBLIC}
        for a in eng.agents.values():
            a.state.subscribed_channels = {name}
            a.state.last_seen_cursor = 0.0
        return eng

    yield build, factory, run_id, name, cid, slack_clients

    async with factory() as db:
        await db.execute(delete(CohortAuditEvent))
        await db.execute(delete(CohortMembership))
        await db.execute(delete(Cohort))
        await db.execute(delete(AgentMessage).where(AgentMessage.simulation_run_id == run_id))
        await db.execute(delete(AgentChannel).where(AgentChannel.simulation_run_id == run_id))
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_(AGENTS)))
        await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


async def _rows(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AgentMessage).where(AgentMessage.simulation_run_id == run_id)
            .order_by(AgentMessage.posted_at))).scalars().all()


# --- T6: restart and reconcile ---------------------------------------------------------


async def test_a_restart_rebuilds_the_log_without_duplicating_it(lifecycle):
    """Two rebuild paths run at startup — the DB rebuild and the Slack reconcile — and
    both append to the same log. `>0` would hide the failure that matters, so the count
    is asserted exactly.
    """
    build, factory, run_id, name, cid, _ = lifecycle
    eng1 = build(slack_on=True)
    await eng1._post_message("su", name, "before the restart")
    time.sleep(POST_GAP)
    await eng1._post_message("cravatt", name, "also before the restart")
    time.sleep(POST_GAP)
    await eng1._flush_persisted()
    assert len(await _rows(factory, run_id)) == 2

    eng2 = build(slack_on=True)
    await eng2._rebuild_state_from_db()
    await eng2._rebuild_state_from_slack()

    contents = sorted(e.content for e in eng2.message_log.get_new_top_level_posts(
        since=0, channels={name}, exclude_agent_id="wiseman", allowed_sender_ids=None))
    assert contents == ["also before the restart", "before the restart"], contents
    assert len(eng2.message_log) == 2, (
        f"the rebuild double-counted: {len(eng2.message_log)} entries for 2 messages"
    )


async def test_a_restart_restores_the_slack_mapping(lifecycle):
    """Without slack_ts on the restored entries, `_slack_parent_ts` reports "no Slack
    root" for every pre-restart thread and silently keeps all subsequent replies off
    Slack. That is invisible from inside the DB."""
    build, factory, run_id, name, cid, _ = lifecycle
    eng1 = build(slack_on=True)
    await eng1._post_message("su", name, "root before restart")
    time.sleep(POST_GAP)
    await eng1._flush_persisted()
    root = (await _rows(factory, run_id))[0]
    assert root.slack_ts

    eng2 = build(slack_on=True)
    await eng2._rebuild_state_from_db()
    assert eng2._slack_parent_ts(root.message_ts) == root.slack_ts, (
        "the restored entry lost its Slack mapping — every later reply in this thread "
        "would silently stop mirroring"
    )

    await eng2._post_message("cravatt", name, "reply after restart",
                             thread_ts=root.message_ts)
    time.sleep(POST_GAP)
    await eng2._flush_persisted()
    live = eng2.slack_clients["su"].get_thread_replies(cid, root.slack_ts)
    assert "reply after restart" in [m.get("text") for m in live]


async def test_a_restart_does_not_repost_to_slack(lifecycle):
    """A reconcile that re-posted restored messages would double every message in the
    channel — visible to the humans reading it, and invisible in our DB."""
    build, factory, run_id, name, cid, slack_clients = lifecycle
    eng1 = build(slack_on=True)
    await eng1._post_message("su", name, "posted once")
    time.sleep(POST_GAP)
    await eng1._flush_persisted()
    before = len(slack_clients["su"].poll_channel_messages(cid, oldest="0"))

    eng2 = build(slack_on=True)
    await eng2._rebuild_state_from_db()
    await eng2._rebuild_state_from_slack()
    time.sleep(POST_GAP)

    after = slack_clients["su"].poll_channel_messages(cid, oldest="0")
    assert len(after) == before, (
        f"the restart posted {len(after) - before} extra message(s) to Slack"
    )
    assert [m.get("text") for m in after].count("posted once") == 1


async def test_ensure_seeded_channels_creates_and_reuses_with_a_live_client(lifecycle):
    """Only the Slack-off branch of _ensure_seeded_channels was covered. With a
    connected client it must create a missing channel and get a real C… id, then REUSE
    it on a second call rather than creating a duplicate."""
    import src.agent.simulation as sim

    build, factory, run_id, name, cid, slack_clients = lifecycle
    fresh = f"t-seeded-{uuid.uuid4().hex[:8]}"
    sim.SEEDED_CHANNELS = [fresh]
    eng = build(slack_on=True)
    try:
        eng._ensure_seeded_channels()
        first = eng._channel_id_map.get(fresh)
        assert first and first.startswith("C"), (
            f"expected a real Slack channel id, got {first!r}"
        )
        assert eng._channel_visibility[fresh] == VISIBILITY_PUBLIC

        eng2 = build(slack_on=True)
        eng2._ensure_seeded_channels()
        assert eng2._channel_id_map.get(fresh) == first, (
            "a second call created a duplicate channel instead of reusing the existing one"
        )
    finally:
        if eng._channel_id_map.get(fresh, "").startswith("C"):
            slack_clients["su"]._call_with_retry(
                slack_clients["su"]._client.conversations_archive,
                channel=eng._channel_id_map[fresh])


# --- T10: Slack-off <-> Slack-on ---------------------------------------------------------


async def test_off_then_on_keeps_old_rows_unmirrored_and_mirrors_new_ones(lifecycle):
    """The realistic hybrid. Rows written while Slack was off must stay slack_ts NULL —
    nothing may be retroactively invented — while new messages mirror normally."""
    build, factory, run_id, name, cid, slack_clients = lifecycle

    off = build(slack_on=False)
    await off._post_message("su", name, "written while slack was off")
    await off._flush_persisted()
    rows = await _rows(factory, run_id)
    assert len(rows) == 1 and rows[0].slack_ts is None, rows[0].slack_ts
    canonical = rows[0].message_ts

    on = build(slack_on=True)
    await on._rebuild_state_from_db()
    await on._post_message("cravatt", name, "written after slack came on")
    time.sleep(POST_GAP)
    await on._flush_persisted()

    by_content = {r.content: r for r in await _rows(factory, run_id)}
    assert by_content["written while slack was off"].slack_ts is None, (
        "a slack_ts was invented for a message Slack never saw"
    )
    assert by_content["written after slack came on"].slack_ts, (
        "control leg failed: the mirror is not working at all after the transition"
    )
    # The off-era root still has no Slack presence, so nothing threads against it.
    assert on._slack_parent_ts(canonical) is None
    live = [m.get("text") for m in slack_clients["su"].poll_channel_messages(cid, oldest="0")]
    assert "written while slack was off" not in live
    assert "written after slack came on" in live


async def test_on_then_off_stops_touching_slack_but_keeps_writing_rows(lifecycle):
    """Control on the negative: assert the Slack channel does NOT grow, rather than
    assuming "no Slack calls" from the absence of an error."""
    build, factory, run_id, name, cid, slack_clients = lifecycle

    on = build(slack_on=True)
    await on._post_message("su", name, "while on")
    time.sleep(POST_GAP)
    await on._flush_persisted()
    before = len(slack_clients["su"].poll_channel_messages(cid, oldest="0"))

    off = build(slack_on=False)
    await off._rebuild_state_from_db()
    await off._post_message("cravatt", name, "while off")
    await off._flush_persisted()
    time.sleep(POST_GAP)

    after = slack_clients["su"].poll_channel_messages(cid, oldest="0")
    assert len(after) == before, (
        f"the Slack-off engine posted {len(after) - before} message(s) to Slack"
    )
    contents = {r.content for r in await _rows(factory, run_id)}
    assert contents == {"while on", "while off"}, (
        f"the DB is not the complete store while Slack is off: {contents}"
    )


# --- T11: failure modes ------------------------------------------------------------------


async def test_a_revoked_token_degrades_to_slack_off_and_keeps_the_row(lifecycle):
    """The one that matters most. The DB is the durable store, so a dead Slack token
    must never cost a message — the turn completes and the row lands.
    """
    build, factory, run_id, name, cid, _ = lifecycle
    from src.agent.slack_client import AgentSlackClient

    eng = build(slack_on=True)
    dead = AgentSlackClient(agent_id="su", bot_token="xoxb-0000-dead-token")
    assert dead.connect() is False, "a bogus token must not authenticate"
    assert dead.is_connected is False
    eng.slack_clients["su"] = dead

    await eng._post_message("su", name, "posted with a dead token")
    await eng._flush_persisted()
    rows = [r for r in await _rows(factory, run_id)
            if r.content == "posted with a dead token"]
    assert rows, "the message was lost when Slack was unavailable"
    assert rows[0].slack_ts is None, "a dead client must not report a Slack ts"


async def test_posting_to_an_archived_channel_does_not_crash(lifecycle, slack_clients):
    """An archived channel is the state every one of this suite's own probe channels
    ends in, so a stale id in _channel_id_map is not hypothetical."""
    build, factory, run_id, name, cid, _ = lifecycle
    su = slack_clients["su"]
    tmp = su.create_channel(f"t-archived-{uuid.uuid4().hex[:8]}")
    assert tmp and tmp.get("id")
    su._call_with_retry(su._client.conversations_archive, channel=tmp["id"])

    eng = build(slack_on=True)
    eng._channel_id_map[tmp["name"]] = tmp["id"]
    eng._channel_visibility[tmp["name"]] = VISIBILITY_PUBLIC
    await eng._post_message("su", tmp["name"], "into the archive")
    await eng._flush_persisted()

    rows = [r for r in await _rows(factory, run_id) if r.content == "into the archive"]
    assert rows, "the message was lost rather than kept in the DB"
    assert rows[0].slack_ts is None


async def test_invite_tolerates_self_and_repeat_but_reports_a_real_failure(slack_clients):
    """`cant_invite_self` and `already_in_channel` are successes by the documented
    contract — "the invite is considered successful as long as every user ends up as a
    member" — and the migration relies on that, since it invites both bots and one of
    them created the channel.

    Control: a genuinely bad user id must still return False, or the tolerance would be
    indistinguishable from a method that always returns True.
    """
    su = slack_clients["su"]
    ch = su.create_private_channel(f"t-selfinvite-{uuid.uuid4().hex[:6]}")
    assert ch and ch.get("id")
    try:
        assert su.invite_to_channel(ch["id"], [su.bot_user_id]) is True
        cravatt_id = slack_clients["cravatt"].bot_user_id
        assert su.invite_to_channel(ch["id"], [cravatt_id]) is True
        assert su.invite_to_channel(ch["id"], [cravatt_id]) is True, "already_in_channel"
        assert su.invite_to_channel(ch["id"], []) is True, "an empty list is a no-op"
        assert su.invite_to_channel(ch["id"], ["U000NOTREAL"]) is False, (
            "a genuine invite failure must be reported"
        )
    finally:
        su._call_with_retry(su._client.conversations_archive, channel=ch["id"])
